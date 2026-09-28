#!/usr/bin/env python3
"""Log into WordPress admin and click "Update" on every post of a given post type.

Usage:
    python bulk_update_posts.py --url https://example.com --username admin --post-type post

Credentials can also come from environment variables (WP_USERNAME / WP_PASSWORD),
or you'll be prompted for a password securely if neither is supplied.

Run with --dry-run first to see which posts would be touched before anything
actually gets saved.
"""

import argparse
import getpass
import os
import sys
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def inject_basic_auth(url: str, basic_auth: tuple[str, str] | None) -> str:
    """Embeds HTTP Basic Auth credentials into a URL's netloc (user:pass@host),
    e.g. for a staging site sitting behind an .htpasswd prompt in front of WordPress
    itself. Once Chrome accepts them for an origin it caches them for the session,
    so later navigations to the same origin don't strictly need this, but applying
    it on every request keeps things robust across origin/redirect changes."""
    if not basic_auth:
        return url

    username, password = basic_auth
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"

    creds = quote(username, safe="")
    if password:
        creds += ":" + quote(password, safe="")

    netloc = f"{creds}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    # Selenium 4.6+ resolves the matching chromedriver automatically.
    return webdriver.Chrome(options=options)


def login(
    driver,
    base_url: str,
    username: str,
    password: str,
    wait: WebDriverWait,
    basic_auth: tuple[str, str] | None = None,
) -> None:
    driver.get(inject_basic_auth(urljoin(base_url, "wp-login.php"), basic_auth))

    wait.until(EC.presence_of_element_located((By.ID, "user_login"))).send_keys(username)
    driver.find_element(By.ID, "user_pass").send_keys(password)
    driver.find_element(By.ID, "wp-submit").click()

    try:
        wait.until(EC.presence_of_element_located((By.ID, "wpadminbar")))
    except TimeoutException:
        error_text = ""
        try:
            error_text = driver.find_element(By.ID, "login_error").text
        except NoSuchElementException:
            pass
        raise RuntimeError(f"Login failed{': ' + error_text if error_text else ''}")


def collect_edit_urls(
    driver,
    base_url: str,
    post_type: str,
    wait: WebDriverWait,
    basic_auth: tuple[str, str] | None = None,
) -> list[str]:
    edit_urls: list[str] = []
    page = 1

    while True:
        list_url = f"{urljoin(base_url, 'wp-admin/edit.php')}?post_type={post_type}&paged={page}"
        driver.get(inject_basic_auth(list_url, basic_auth))

        try:
            wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#the-list, .wrap"))
            )
        except TimeoutException:
            break

        rows = driver.find_elements(By.CSS_SELECTOR, "#the-list tr")
        if not rows:
            break

        links = driver.find_elements(By.CSS_SELECTOR, "#the-list a.row-title")
        if not links:
            break

        page_urls = [a.get_attribute("href") for a in links]
        edit_urls.extend(page_urls)

        # Stop once pagination stops advancing (last page reached).
        next_link = driver.find_elements(By.CSS_SELECTOR, ".tablenav-pages a.next-page")
        if not next_link:
            break
        page += 1

    return edit_urls


def get_post_title(driver) -> str | None:
    """Reads the current post title from either the block editor or classic editor."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, ".editor-post-title__input")
        value = el.get_attribute("value")
        return value if value else el.text
    except NoSuchElementException:
        pass

    try:
        return driver.find_element(By.ID, "title").get_attribute("value")
    except NoSuchElementException:
        return None


def set_field_value(driver, element, value: str) -> None:
    element.clear()
    element.send_keys(value)
    # ACF's own JS (conditional logic, validation, etc.) listens for these events,
    # which send_keys doesn't reliably fire on every field type.
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
        "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
        element,
    )


def copy_title_into_acf_field(
    driver, wait: WebDriverWait, tab_selector: str | None, field_selector: str
) -> bool:
    """Copies the post title into an ACF field, clicking its tab first if needed
    (ACF hides fields on inactive tabs, so the field may not be interactable until
    its tab is selected)."""
    title = get_post_title(driver)
    if not title:
        return False

    if tab_selector:
        try:
            tab = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, tab_selector)))
            tab.click()
        except TimeoutException:
            pass  # tab may already be active, or not present on this particular post

    try:
        field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, field_selector)))
    except TimeoutException:
        return False

    set_field_value(driver, field, title)
    return True


def click_update_button(driver, wait: WebDriverWait) -> str:
    """Handles both the classic editor and the block (Gutenberg) editor."""
    try:
        btn = wait.until(EC.element_to_be_clickable((By.ID, "publish")))
        btn.click()
        return "classic"
    except TimeoutException:
        pass

    gutenberg_selectors = [
        ".editor-post-publish-button__button",
        "button.editor-post-publish-button",
        "button[aria-label='Update']",
    ]
    for selector in gutenberg_selectors:
        try:
            btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            btn.click()
            return "gutenberg"
        except TimeoutException:
            continue

    raise RuntimeError("Could not find an Update/Publish button on this edit screen")


def wait_for_save(driver, editor_kind: str, wait: WebDriverWait) -> bool:
    try:
        if editor_kind == "classic":
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#message.updated, .notice-success")))
        else:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".components-snackbar, .editor-post-saved-state.is-saved")
                )
            )
        return True
    except TimeoutException:
        return False


def update_post(
    driver,
    edit_url: str,
    wait: WebDriverWait,
    acf_options: dict | None = None,
    basic_auth: tuple[str, str] | None = None,
) -> tuple[bool, bool | None]:
    driver.get(inject_basic_auth(edit_url, basic_auth))
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    acf_ok = None
    if acf_options:
        acf_ok = copy_title_into_acf_field(
            driver, wait, acf_options.get("tab_selector"), acf_options["field_selector"]
        )

    editor_kind = click_update_button(driver, wait)
    saved = wait_for_save(driver, editor_kind, wait)
    return saved, acf_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Site base URL, e.g. https://example.com")
    parser.add_argument("--username", default=os.environ.get("WP_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("WP_PASSWORD"))
    parser.add_argument(
        "--basic-auth-username",
        default=os.environ.get("BASIC_AUTH_USERNAME"),
        help="Username for an HTTP Basic Auth prompt in front of the site (e.g. staging .htpasswd)",
    )
    parser.add_argument(
        "--basic-auth-password",
        default=os.environ.get("BASIC_AUTH_PASSWORD"),
        help="Password for HTTP Basic Auth (prompted securely if --basic-auth-username is set but this isn't)",
    )
    parser.add_argument("--post-type", default="post", help="Post type slug (post, page, or a custom type)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds to pause between posts")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N posts (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="List posts that would be updated, without clicking Update")
    parser.add_argument(
        "--copy-title-to-acf",
        action="store_true",
        help="Before saving, copy the post title into an ACF field (see --acf-field-selector)",
    )
    parser.add_argument(
        "--acf-tab-selector",
        default="#acf-cpt-member-news > div.inside.acf-fields.-top.-sidebar > div.acf-tab-wrap.-left > ul > li.active > a",
        help="CSS selector for the ACF sidebar tab to click before filling the field "
        "(ACF hides fields on tabs that aren't active)",
    )
    parser.add_argument(
        "--acf-field-selector",
        default="#acf-field_member_news_hero_clone-field_member_news_hero_clone_hero_member_news_title",
        help="CSS selector (usually the field's #id) for the ACF field to fill with the post title",
    )
    args = parser.parse_args()

    if not args.username:
        args.username = input("WordPress username: ")
    if not args.password:
        args.password = getpass.getpass("WordPress password: ")

    basic_auth = None
    if args.basic_auth_username:
        if not args.basic_auth_password:
            args.basic_auth_password = getpass.getpass("Basic Auth password: ")
        basic_auth = (args.basic_auth_username, args.basic_auth_password)

    base_url = args.url if args.url.endswith("/") else args.url + "/"

    driver = build_driver(args.headless)
    wait = WebDriverWait(driver, 20)

    try:
        login(driver, base_url, args.username, args.password, wait, basic_auth)
        print("Logged in.")

        edit_urls = collect_edit_urls(driver, base_url, args.post_type, wait, basic_auth)
        if args.limit:
            edit_urls = edit_urls[: args.limit]

        print(f"Found {len(edit_urls)} '{args.post_type}' post(s).")

        if args.dry_run:
            for url in edit_urls:
                print(f"[dry-run] {url}")
            return 0

        acf_options = None
        if args.copy_title_to_acf:
            acf_options = {
                "tab_selector": args.acf_tab_selector,
                "field_selector": args.acf_field_selector,
            }

        succeeded, failed = 0, 0
        for i, url in enumerate(edit_urls, start=1):
            print(f"[{i}/{len(edit_urls)}] Updating {url}")
            try:
                ok, acf_ok = update_post(driver, url, wait, acf_options, basic_auth)
                if acf_options and not acf_ok:
                    print("  -> warning: could not locate/fill the ACF field")
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                    print(f"  -> could not confirm save for {url}")
            except (TimeoutException, WebDriverException, RuntimeError) as exc:
                failed += 1
                print(f"  -> error: {exc}")
            time.sleep(args.delay)

        print(f"Done. {succeeded} updated, {failed} failed.")
        return 0 if failed == 0 else 1
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
