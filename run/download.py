"""download.py: stream one pinned file over http(s) or file://, checking its size and sha256 as it arrives, with retries
on a dropped connection or a server failure. Stdlib only: the fetchers (LDBC's fetch, python/kvevaluator/datasets/ldbc/fetch.py, and fetch_results.py) load it by
file before the Python environment exists."""
import hashlib, http.client, time, urllib.error, urllib.request

ATTEMPTS = 3   # a host may ignore Range, so a dropped download restarts from byte 0


class Dropped(OSError):
    """a download that broke off part way; worth another attempt"""


def download_retrying(url, dest, wait, agent, hint, **want):
    """download(), up to ATTEMPTS times when the connection drops or the server fails (5xx); a 4xx or a wrong file is
    not retried. hint: what to do when every attempt fails (said after the last error)"""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            print(f"    attempt {attempt}/{ATTEMPTS}", flush=True)
            return download(url, dest, agent, **want)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise                                      # a 404 is an answer, not a failure
            why = f"HTTP {e.code}"
        except (Dropped, urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
            why = str(e)
        if attempt == ATTEMPTS:
            raise Dropped(f"{ATTEMPTS} attempts failed, the last: {why}. {hint}")
        print(f"    attempt {attempt}/{ATTEMPTS} failed: {why}; again in {wait:g} s", flush=True)
        time.sleep(wait)


def download(url, dest, agent, want_size=None, want_sha=None):
    """stream url to dest, hashing as it goes; returns (size, sha256). Refuses a size or sha256 other than the pinned one.
    agent: sent as User-Agent (some hosts refuse Python's default)"""
    h, n, t, last = hashlib.sha256(), 0, time.time(), 0
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        if want_size is not None and total and total != want_size:
            raise ValueError(f"the host serves {total} bytes, the manifest pins {want_size}")
        while True:
            try:
                block = r.read(1 << 20)                    # only the read is a network failure; a write error is not retried
            except (OSError, http.client.HTTPException) as e:
                raise Dropped(f"{type(e).__name__}: {e} after {n} bytes, {time.time() - t:.0f} s") from e
            if not block:
                break
            h.update(block)
            out.write(block)
            n += len(block)
            if total and n * 10 // total > last:
                last = n * 10 // total
                print(f"    {n / 1e6:.0f} / {total / 1e6:.0f} MB ({time.time() - t:.0f} s)", flush=True)
        if total and n != total:
            raise Dropped(f"the stream ended after {n} of {total} bytes, {time.time() - t:.0f} s")
    print(f"    {n / 1e6:.1f} MB in {time.time() - t:.0f} s ({n / 1e6 / max(time.time() - t, 1e-9):.1f} MB/s)", flush=True)
    if want_size is not None and n != want_size:
        raise ValueError(f"downloaded {n} bytes, the manifest pins {want_size}")
    if want_sha is not None and h.hexdigest() != want_sha:
        raise ValueError(f"sha256 {h.hexdigest()}, the manifest pins {want_sha}")
    return n, h.hexdigest()
