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

```bash
pip install -r requirements.txt
```

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

### Options

| Flag | Default | Description |
|---|---|---|
| `--url` | *(required)* | Site base URL, e.g. `https://example.com` |
| `--username` | `$WP_USERNAME` or prompt | WordPress username |
| `--password` | `$WP_PASSWORD` or prompt | WordPress password |
| `--post-type` | `post` | Post type slug (`post`, `page`, or a custom type) |
| `--headless` | off | Run Chrome headless |
| `--delay` | `1.5` | Seconds to pause between posts |
| `--limit` | none | Only process the first N posts (useful for testing) |
| `--dry-run` | off | List posts that would be updated, without clicking Update |
| `--copy-title-to-acf` | off | Copy the post title into an ACF field before saving |
| `--acf-tab-selector` | see below | CSS selector for the ACF sidebar tab to click before filling the field |
| `--acf-field-selector` | see below | CSS selector (usually an `#id`) for the ACF field to fill with the post title |

### ACF title copy

`--copy-title-to-acf` reads the post title (from either editor), clicks the
ACF tab that contains the target field (ACF hides fields on inactive tabs),
fills the field, and fires `input`/`change` events so ACF's own JS
(conditional logic, validation) picks it up — all before the Update click.

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
