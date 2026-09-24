#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fetch URLs via proxy, decode, strip HTML -> txt files."""
import subprocess, sys, os, re, html

OUT = r"C:\Users\zjjem\.search_tmp\inner_train\pages"
os.makedirs(OUT, exist_ok=True)

env = dict(os.environ)
env["HTTP_PROXY"] = "http://127.0.0.1:33210"
env["HTTPS_PROXY"] = "http://127.0.0.1:33210"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

items = []
for a in sys.argv[1:]:
    name, url = a.split("|", 1)
    items.append((name, url))


def strip(h):
    h = re.sub(r"(?is)<script.*?</script>", " ", h)
    h = re.sub(r"(?is)<style.*?</style>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?is)<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n", h)
    return h.strip()


def sniff(raw):
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except Exception:
        return raw.decode("utf-8", "replace")


for name, url in items:
    fn = os.path.join(OUT, name + ".txt")
    try:
        r = subprocess.run(["curl", "-sL", "--compressed", "-m", "40", "-A", UA, url],
                           capture_output=True, timeout=70, env=env)
        raw = r.stdout
        if not raw:
            print("EMPTY", name, url); continue
        text = strip(sniff(raw))
        open(fn, "w", encoding="utf-8").write(url + "\n\n" + text)
        print("OK %-28s %6d chars" % (name, len(text)))
    except Exception as e:
        print("ERR", name, repr(e)[:100])
