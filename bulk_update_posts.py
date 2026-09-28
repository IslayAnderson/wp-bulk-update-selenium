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
import base64
import getpass
import os
import re
import sys
import time
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def apply_basic_auth(driver, basic_auth: tuple[str, str] | None) -> None:
    """Sets an HTTP Basic Auth header via CDP for every request the browser makes.

    Embedding credentials in the URL (user:pass@host) is unreliable on modern
    Chrome: it's blocked/stripped on top-level navigation as an anti-phishing
    measure, so it silently fails to authenticate. Setting the Authorization
    header directly bypasses that entirely and applies for the rest of the
    session, so callers only need to call this once, up front.
    """
    if not basic_auth:
        return

    username, password = basic_auth
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd(
        "Network.setExtraHTTPHeaders", {"headers": {"Authorization": f"Basic {token}"}}
    )


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    # Selenium 4.6+ resolves the matching chromedriver automatically.
    return webdriver.Chrome(options=options)


def safe_click(driver, element) -> None:
    """Clicks an element, falling back to a JS click if something is covering it
    (an overlay, a stale layout reflow, etc.) that a real click can't get through."""
    try:
        element.click()
    except (ElementNotInteractableException, ElementClickInterceptedException):
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        driver.execute_script("arguments[0].click();", element)


def post_is_locked(driver) -> bool:
    """True if WordPress is showing its 'this post is being edited by another
    user' takeover dialog. That dialog visually covers the real Update button,
    which is why a click on it raises ElementNotInteractable rather than just
    not finding it — the button is still there in the DOM, just unreachable."""
    try:
        dialog = driver.find_element(By.ID, "post-lock-dialog")
        return dialog.is_displayed()
    except NoSuchElementException:
        return False


def login(driver, base_url: str, username: str, password: str, wait: WebDriverWait) -> None:
    driver.get(urljoin(base_url, "wp-login.php"))

    # Use the `name` attributes rather than ids/labels: WP core always renders
    # name="log"/name="pwd"/name="wp-submit" on the login form even when a theme
    # restyles the markup around it (custom labels, wrapper classes, etc.).
    wait.until(EC.presence_of_element_located((By.NAME, "log"))).send_keys(username)
    driver.find_element(By.NAME, "pwd").send_keys(password)

    try:
        submit = driver.find_element(By.NAME, "wp-submit")
    except NoSuchElementException:
        submit = driver.find_element(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")
    safe_click(driver, submit)

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
    driver, base_url: str, post_type: str, wait: WebDriverWait
) -> tuple[list[str], int | None]:
    """Returns (edit_urls, reported_total). reported_total comes from WordPress's
    own "N items" count in the list table header, when available, so callers can
    tell if the crawl stopped short of everything the site says exists."""
    edit_urls: list[str] = []
    reported_total: int | None = None
    page = 1

    while True:
        list_url = f"{urljoin(base_url, 'wp-admin/edit.php')}?post_type={post_type}&paged={page}"
        driver.get(list_url)

        try:
            wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#the-list, .wrap"))
            )
        except TimeoutException:
            break

        if reported_total is None:
            try:
                displaying_num = driver.find_element(By.CSS_SELECTOR, ".displaying-num").text
                match = re.search(r"[\d,]+", displaying_num)
                if match:
                    reported_total = int(match.group().replace(",", ""))
            except NoSuchElementException:
                pass

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

    return edit_urls, reported_total


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
            safe_click(driver, tab)
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
        safe_click(driver, btn)
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
            safe_click(driver, btn)
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
    driver, edit_url: str, wait: WebDriverWait, acf_options: dict | None = None
) -> tuple[bool, bool | None]:
    driver.get(edit_url)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    if post_is_locked(driver):
        raise RuntimeError(
            "Post is locked (currently being edited by another user) — skipped rather than taking it over"
        )

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
    parser.add_argument(
        "--wp-path",
        default=os.environ.get("WP_PATH", ""),
        help="Path prefix where WordPress core lives under --url, e.g. 'wp' if the "
        "dashboard is at https://example.com/wp/wp-admin/ instead of the site root",
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
    if args.wp_path:
        base_url = urljoin(base_url, args.wp_path.strip("/") + "/")

    driver = build_driver(args.headless)
    wait = WebDriverWait(driver, 20)

    try:
        apply_basic_auth(driver, basic_auth)
        login(driver, base_url, args.username, args.password, wait)
        print("Logged in.")

        edit_urls, reported_total = collect_edit_urls(driver, base_url, args.post_type, wait)
        crawled_count = len(edit_urls)
        if args.limit:
            edit_urls = edit_urls[: args.limit]

        print(f"Found {crawled_count} '{args.post_type}' post(s).")
        if reported_total is not None and reported_total != crawled_count:
            print(
                f"  -> warning: WordPress's list table reports {reported_total} item(s) "
                f"in this view, but the crawl collected {crawled_count}. Check the post "
                "list's status/filter tabs (All/Published/Draft/Trash) and pagination "
                "before running for real — this crawl may have stopped early."
            )

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
                ok, acf_ok = update_post(driver, url, wait, acf_options)
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
