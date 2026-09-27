#!/usr/bin/env python3
"""Auto-translate Odoo custom-module ``.pot`` files with a LibreTranslate server.

This is the "future words" tool: whenever new English strings are added to a
custom module, re-export its ``.po`` template and run this script again. Only
the missing terms are translated; existing translations and the LibreTranslate
cache are reused.

Markup safety (machine translation is never blindly trusted):
    * plain strings are translated in ``text`` mode;
    * strings containing HTML are translated in ``html`` mode, which keeps the
      tags in place;
    * every result is validated: placeholders (``%s``, ``%(name)s``, ``{0}``,
      ``${...}``) and HTML tags must survive untouched, otherwise the
      translation is dropped and the English source is kept.

Plural entries are left untranslated on purpose.

Usage:
    python3 auto_translate.py --pot /tmp/pots/s_odoo_saas_master.pot \
        --out-dir <module>/i18n --langs fr,es \
        --url http://127.0.0.1:5001

Then reload the terms in Odoo:
    odoo-bin -c /etc/odoo18.conf -d <db> --load-language=fr_FR,es_ES \
        --stop-after-init --no-http
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

try:
    import polib
except ImportError:  # pragma: no cover
    sys.stderr.write("This tool needs the 'polib' package (pip install polib).\n")
    raise

RE_TAG = re.compile(r"<[^>]+>")
RE_TAG_OPEN = re.compile(r"<([a-zA-Z][\w:-]*)((?:\s+[^<>]*?)?)(/?)>")
RE_TAG_CLOSE = re.compile(r"</\s*([a-zA-Z][\w:-]*)\s*>")
RE_TAG_ATTR = re.compile(r"([a-zA-Z_:][-\w:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+)")
RE_PLACEHOLDER = re.compile(r"%\([^)]+\)[sdifr]|%[sdifr]|\$\{[^}]+\}|\{[0-9]*\}")
RE_LETTER = re.compile(r"[A-Za-z]")


def contains_html(msgid):
    return bool(RE_TAG.search(msgid))


def is_candidate(msgid):
    """Return True when a string is worth sending to the translator."""
    if not msgid or not msgid.strip():
        return False
    if len(msgid) > 3000:
        return False
    if not RE_LETTER.search(msgid):
        return False
    return True


def placeholders(text):
    return Counter(RE_PLACEHOLDER.findall(text))


def tags(text):
    """Normalised multiset of tags (name + attribute set).

    LibreTranslate preserves the markup but may reorder attributes or expand a
    self-closing tag (``<i/>`` -> ``<i></i>``).  Comparing raw tag strings
    would then reject perfectly safe translations, so tags are compared by
    name and attribute set instead.
    """
    result = Counter()
    for token in RE_TAG.findall(text):
        if token.startswith("</"):
            match = RE_TAG_CLOSE.match(token)
            if match:
                result[("close", match.group(1).lower())] += 1
            continue
        match = RE_TAG_OPEN.match(token)
        if not match:
            continue
        name = match.group(1).lower()
        attrs = tuple(sorted(
            (attr.lower(), value)
            for attr, value in RE_TAG_ATTR.findall(match.group(2) or "")
        ))
        result[("open", name, attrs)] += 1
        if match.group(3) == "/":
            result[("close", name)] += 1
    return result


def is_translation_safe(source, target, html):
    """A translation is kept only when placeholders/tags survived."""
    if target is None:
        return False
    if placeholders(source) != placeholders(target):
        return False
    if html and tags(source) != tags(target):
        return False
    return True


def lt_translate(texts, target, url, timeout, fmt="text", api_key=None):
    """Translate a list of strings with LibreTranslate, return a list."""
    payload = {"q": texts, "source": "en", "target": target, "format": fmt}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(
        url.rstrip("/") + "/translate",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    translated = result.get("translatedText")
    if isinstance(translated, str):
        return [translated]
    if not isinstance(translated, list):
        raise ValueError("Unexpected LibreTranslate response: %r" % (result,))
    return translated


def load_json(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)


def run_group(entries, lang, fmt, args, cache):
    """Translate one group of same-format entries and store safe results."""
    if not entries:
        return
    texts = [entry.msgid for entry in entries]
    sys.stderr.write(
        "[%s/%s] translating %d string(s)\n" % (lang, fmt, len(texts))
    )
    results = []
    for start in range(0, len(texts), args.batch_size):
        chunk = texts[start : start + args.batch_size]
        try:
            batch = lt_translate(chunk, lang, args.url, args.timeout, fmt, args.api_key)
            if len(batch) != len(chunk):
                raise ValueError("batch returned %d for %d inputs" % (len(batch), len(chunk)))
            results.extend(batch)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            sys.stderr.write("[%s/%s] batch failed (%s); single calls\n" % (lang, fmt, exc))
            for text in chunk:
                try:
                    results.extend(lt_translate([text], lang, args.url, args.timeout, fmt, args.api_key))
                except Exception as single_exc:  # noqa: BLE001
                    sys.stderr.write("[%s/%s]   single failed: %s\n" % (lang, fmt, single_exc))
                    results.append(None)
        done = min(start + args.batch_size, len(texts))
        sys.stderr.write("[%s/%s] %d/%d\n" % (lang, fmt, done, len(texts)))
        time.sleep(args.sleep)

    kept = 0
    for entry, value in zip(entries, results):
        if value and value.strip() and is_translation_safe(entry.msgid, value.strip(), fmt == "html"):
            entry.msgstr = value.strip()
            cache[lang][entry.msgid] = entry.msgstr
            kept += 1

    # LibreTranslate often returns the source unchanged for Title Case strings
    # (it treats them as proper nouns).  Retry those in lower case and keep the
    # result only when it is a real, markup-safe translation.
    retried = 0
    for entry in entries:
        if entry.msgstr and entry.msgstr.strip() != entry.msgid.strip():
            continue
        lowered = entry.msgid.lower()
        if lowered.strip() == entry.msgid.strip():
            continue
        try:
            single = lt_translate([lowered], lang, args.url, args.timeout, fmt, args.api_key)
        except Exception:  # noqa: BLE001
            continue
        value = single[0] if isinstance(single, list) and single else None
        if not value or not value.strip():
            continue
        value = value.strip()
        if value.lower() == entry.msgid.lower():
            continue
        if not is_translation_safe(entry.msgid, value, fmt == "html"):
            continue
        if value[:1].isalpha():
            value = value[:1].upper() + value[1:]
        entry.msgstr = value
        cache[lang][entry.msgid] = value
        retried += 1
    sys.stderr.write(
        "[%s/%s] kept %d/%d (+%d lower-case retry)\n" % (lang, fmt, kept, len(entries), retried)
    )


def translate_language(pot, lang, args, cache):
    po_path = os.path.join(args.out_dir, "%s.po" % lang)
    existing = {}
    if os.path.exists(po_path):
        for entry in polib.pofile(po_path):
            if entry.msgid and entry.msgstr and not entry.obsolete:
                existing[entry.msgid] = entry.msgstr

    out = polib.POFile()
    out.metadata = {
        "Project-Id-Version": "Odoo Server 18.0",
        "Report-Msgid-Bugs-To": "",
        "POT-Creation-Date": "",
        "PO-Revision-Date": time.strftime("%Y-%m-%d %H:%M%z"),
        "Last-Translator": "auto_translate / LibreTranslate (machine translated)",
        "Language-Team": lang,
        "Language": lang,
        "MIME-Version": "1.0",
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
        "Plural-Forms": "nplurals=2; plural=(n > 1);",
    }

    cache.setdefault(lang, {})
    pending = []
    for entry in pot:
        if entry.obsolete or not entry.msgid:
            continue
        new_entry = polib.POEntry(
            msgid=entry.msgid,
            msgid_plural=entry.msgid_plural,
            occurrences=list(entry.occurrences),
            comment=entry.comment,
            tcomment=entry.tcomment,
            flags=list(entry.flags),
        )
        if entry.msgid_plural:
            out.append(new_entry)
            continue
        reused = None
        if not args.force:
            reused = existing.get(entry.msgid) or cache[lang].get(entry.msgid)
        if reused and reused.strip() != entry.msgid.strip():
            new_entry.msgstr = reused
        elif is_candidate(entry.msgid):
            pending.append(new_entry)
        out.append(new_entry)

    if args.limit:
        pending = pending[: args.limit]

    if not pending:
        sys.stderr.write("[%s] nothing new to translate\n" % lang)

    html_entries = [entry for entry in pending if contains_html(entry.msgid)]
    text_entries = [entry for entry in pending if not contains_html(entry.msgid)]
    run_group(html_entries, lang, "html", args, cache)
    run_group(text_entries, lang, "text", args, cache)

    out.save(po_path)
    translated_count = sum(1 for e in out if e.msgstr)
    sys.stderr.write(
        "[%s] wrote %s (%d/%d translated)\n" % (lang, po_path, translated_count, len(out))
    )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pot", required=True, help="Path to the module .pot file")
    parser.add_argument("--out-dir", required=True, help="Directory that will receive <lang>.po (usually <module>/i18n)")
    parser.add_argument("--langs", default="fr,es", help="Comma separated target language codes (default: fr,es)")
    parser.add_argument("--url", default="http://127.0.0.1:5001", help="LibreTranslate server URL")
    parser.add_argument("--api-key", default=None, help="Optional LibreTranslate API key")
    parser.add_argument("--cache", default=None, help="Cache file (default: <out-dir>/.auto_translate_cache.json)")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sleep", type=float, default=0.1, help="Pause between batches")
    parser.add_argument("--limit", type=int, default=0, help="Only translate the first N missing strings (debug)")
    parser.add_argument("--force", action="store_true", help="Re-translate everything, ignoring existing translations")
    args = parser.parse_args()

    if not os.path.exists(args.pot):
        parser.error("POT file not found: %s" % args.pot)
    os.makedirs(args.out_dir, exist_ok=True)

    cache_path = args.cache or os.path.join(args.out_dir, ".auto_translate_cache.json")
    cache = load_json(cache_path)
    pot = polib.pofile(args.pot)

    try:
        for lang in [item.strip() for item in args.langs.split(",") if item.strip()]:
            translate_language(pot, lang, args, cache)
            save_json(cache_path, cache)
    except KeyboardInterrupt:
        save_json(cache_path, cache)
        sys.stderr.write("\ninterrupted; cache saved, re-run to continue\n")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
