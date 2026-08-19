"""A consistent copy of a live WAL SQLite database, without installing anything.

`VACUUM INTO` takes a read transaction and writes a compacted, self-consistent
copy while the writer keeps running. Unlike every other engine here it cannot
stream to stdout: it must write a file on the *source* host, which is then
pulled and removed.

The program below is piped to the source's own `python3` — the `sqlite3`
module is standard library, so the source host stays a bare SSH endpoint with
nothing installed and nothing to keep in sync.

It prints one JSON object on stdout and nothing else.
"""

from __future__ import annotations

# Kept as a string rather than a file so there is nothing to deploy, and so
# the exact text is greppable from the manifest.
PROGRAM = r'''
import json, os, sqlite3, sys, urllib.parse

src, tmp = sys.argv[1], sys.argv[2]

# VACUUM INTO refuses to overwrite, so a leftover from a killed run would break
# every subsequent run. Say why, rather than letting sqlite say "file exists".
if os.path.exists(tmp):
    sys.exit(
        "refusing to run: %s already exists. It is left over from an "
        "interrupted run; inspect and remove it." % tmp
    )

os.makedirs(os.path.dirname(tmp), exist_ok=True)

need = os.path.getsize(src)
st = os.statvfs(os.path.dirname(tmp))
free = st.f_bavail * st.f_frsize
if free < need:
    sys.exit("insufficient space: need %d bytes, %s has %d" % (need, os.path.dirname(tmp), free))

uri = "file:" + urllib.parse.quote(src) + "?mode=ro"
try:
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("VACUUM INTO ?", (tmp,))
    finally:
        con.close()

    check = sqlite3.connect("file:" + urllib.parse.quote(tmp) + "?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise RuntimeError("integrity_check on the copy: " + result)
except BaseException:
    # A half-written multi-gigabyte file left behind every night is its own
    # outage, so failure cleans up after itself as well as success does.
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise

print(json.dumps({
    "source_bytes": need,
    "bytes": os.path.getsize(tmp),
    "integrity_check": result,
}))
'''
