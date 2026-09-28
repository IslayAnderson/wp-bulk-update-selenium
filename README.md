# wp-bulk-update-selenium

A Selenium script that logs into WordPress admin, crawls all posts of a given
post type, and clicks **Update** on each one. Optionally copies the post
title into an ACF field before saving — handy for backfilling a clone-field
title from the main post title across many posts.

Works with both the classic editor and the block (Gutenberg) editor.

## Requirements

- Python 3.10+
- Google Chrome installed locally (Selenium 4.6+ auto-resolves the matching
  chromedriver, so no separate driver install is needed)

## Install

Create a virtual environment and install dependencies into it:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`.venv/` is already excluded via `.gitignore`. Once installed, run the
script with that environment active (`source .venv/bin/activate` in any new
shell before running it — `deactivate` when you're done).

## Usage

```bash
python bulk_update_posts.py --url https://example.com --username admin --post-type post --dry-run
```

Always run with `--dry-run` first — it logs in, crawls the post list, and
prints the edit URLs it *would* update, without clicking anything.

Once the list looks right, drop `--dry-run` to actually run it:

```bash
python bulk_update_posts.py --url https://example.com --username admin --post-type post
```

### Credentials

Pass `--username`/`--password`, or set them as environment variables so they
don't end up in shell history:

```bash
export WP_USERNAME=admin
export WP_PASSWORD='...'
python bulk_update_posts.py --url https://example.com --post-type post
```

If neither is supplied you'll be prompted (password entry is hidden).

### HTTP Basic Auth

If the site (e.g. a staging server) sits behind an `.htpasswd`-style Basic
Auth prompt in front of WordPress itself, supply those separate credentials
with `--basic-auth-username`/`--basic-auth-password`, or via environment
variables:

```bash
export BASIC_AUTH_USERNAME=staging
export BASIC_AUTH_PASSWORD='...'
python bulk_update_posts.py --url https://staging.example.com --post-type post
```

If `--basic-auth-username` is set but no password is given, you'll be
prompted for one securely. These credentials are sent as an `Authorization`
header on every request via Chrome DevTools Protocol (set once at startup),
not embedded in the URL — modern Chrome blocks/strips `user:pass@host` URLs
on top-level navigation as an anti-phishing measure, so that approach
silently fails to authenticate.

### WordPress in a subdirectory

If the dashboard isn't at the site root — e.g. `https://example.com/wp/wp-admin/`
instead of `https://example.com/wp-admin/` — pass the subdirectory with
`--wp-path` (or `$WP_PATH`) rather than baking it into `--url`:

```bash
python bulk_update_posts.py --url https://example.com --wp-path wp --post-type post
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--url` | *(required)* | Site base URL, e.g. `https://example.com` |
| `--username` | `$WP_USERNAME` or prompt | WordPress username |
| `--password` | `$WP_PASSWORD` or prompt | WordPress password |
| `--basic-auth-username` | `$BASIC_AUTH_USERNAME` | Username for an HTTP Basic Auth prompt in front of the site |
| `--basic-auth-password` | `$BASIC_AUTH_PASSWORD` or prompt | Password for HTTP Basic Auth |
| `--wp-path` | `$WP_PATH` or empty | Path prefix where WordPress core lives, e.g. `wp` |
| `--post-type` | `post` | Post type slug (`post`, `page`, or a custom type) |
| `--headless` | off | Run Chrome headless |
| `--delay` | `1.5` | Seconds to pause between posts |
| `--limit` | none | Only process the first N posts (useful for testing) |
| `--dry-run` | off | List posts that would be updated, without clicking Update |
| `--copy-title-to-acf` | off | Copy the post title into an ACF field before saving |
| `--acf-tab-selector` | see below | CSS selector for the ACF sidebar tab to click before filling the field |
| `--acf-field-selector` | see below | CSS selector (usually an `#id`) for the ACF field to fill with the post title |

### ACF title copy

`--copy-title-to-acf` reads the post title (from either editor), best-effort
clicks the ACF tab that contains the target field (purely cosmetic — see
below), then sets the field's value via JavaScript and fires `input`/`change`
events so ACF's own JS (conditional logic, validation) picks it up — all
before the Update click.

The value is set via JS rather than simulated typing because
`--acf-tab-selector` (as given) targets *whichever* tab happens to already be
active, not necessarily the one the target field lives on — if it's the
wrong tab, the field's pane stays hidden (`display:none`), and a real
click/type on a hidden element throws `ElementNotInteractable`. Setting
`.value` directly works regardless of which tab is showing, so the tab click
failing (or being a no-op) no longer breaks the run.

The default selectors target a specific clone field on the `member_news`
post type:

```
--acf-tab-selector "#acf-cpt-member-news > div.inside.acf-fields.-top.-sidebar > div.acf-tab-wrap.-left > ul > li.active > a"
--acf-field-selector "#acf-field_member_news_hero_clone-field_member_news_hero_clone_hero_member_news_title"
```

Override both for other post types/fields, e.g.:

```bash
python bulk_update_posts.py --url https://example.com --post-type page \
  --copy-title-to-acf \
  --acf-tab-selector "#acf-some-group .acf-tab-wrap li.active > a" \
  --acf-field-selector "#acf-field_some_key"
```

Combine with `--limit 3` and `--headless` off the first time you try it
against a new field, so you can watch the browser confirm it's filling the
right input.

## Notes

- Re-saving every post triggers each post's normal `save_post` hooks (cache
  purges, search reindexing, etc.) — check with whoever manages the site's
  caching/CDN layer before running this against a large post type in
  production.
- If a post's tab layout or ACF field markup differs from the defaults,
  `click_update_button`/`copy_title_into_acf_field` will log a warning for
  that post and move on rather than stopping the whole run.
- If WordPress's own "N items" count on the post list doesn't match what the
  crawl collected, a warning is printed after the "Found N posts" line — a
  mismatch usually means the list is filtered to a different status/view
  (All/Published/Draft/Trash) than you expect, or that pagination stopped
  earlier than it should have.
- If a post is locked (someone else has it open in the editor), WordPress
  shows a takeover dialog over the real Update button. The script detects
  this and skips that post with a clear warning rather than clicking through
  it and taking over someone else's edit session.
