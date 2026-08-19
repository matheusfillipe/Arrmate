"""Composite diagnosis playbooks exposed as agent tools.

The model decides *when* to run these; deterministic code decides *how*.
Each playbook runs a fixed multi-step procedure across services and returns
a structured finding, so the recurring diagnoses are fast, cheap, and
consistent instead of depending on the model improvising a tool sequence.
"""

import logging
import re
from typing import Any

import httpx
from pydantic_ai import Agent, RunContext

from arrmate.config.settings import settings

from .deps import AgentDeps
from .tools import _wrap

logger = logging.getLogger(__name__)

_SUSPICIOUS_EXTENSIONS = (".exe", ".lnk", ".scr", ".zipx", ".com", ".pif", ".vbs", ".bat")

_MANUAL_FAIL_MARKERS = ("Manually marked as failed", "manually marked")

_EDITION_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(part \d+ of \d+|dramatized|unabridged|abridged|audiobook|"
    r"adaptation|edition|movie tie-in)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)

_MIN_AUDIOBOOK_BYTES = 20 * 1024 * 1024

_VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".avi",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".webm",
)


def _search_queries(title: str, author: str) -> list[str]:
    """Query variants for one book, most specific first.

    Listenarr searches its indexers for the full Audible title plus the author,
    which matches nothing when the title carries an edition parenthetical or the
    author never appears in release names (upstream issues #801, #527).
    """
    clean = _EDITION_NOISE.sub("", title or "").strip()
    variants = [f"{clean} {author}".strip(), clean, (title or "").strip()]
    seen: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.append(v)
    return seen


def _rank_releases(results: list[dict], title: str) -> list[dict]:
    """Plausible audiobook releases for a title, best first.

    Filters the lesson clips and sample files that dominate language-learning
    queries, requires the distinctive words of the title to be present, then
    prefers seeders.
    """
    words = [w for w in re.split(r"\W+", _EDITION_NOISE.sub("", title or "").lower()) if len(w) > 3]
    keep = []
    for r in results:
        name = (r.get("title") or "").lower()
        if (r.get("size") or 0) < _MIN_AUDIOBOOK_BYTES:
            continue
        if words and not all(w in name for w in words):
            continue
        if not r.get("downloadReference"):
            continue
        keep.append(r)
    return sorted(keep, key=lambda r: r.get("seeders") or 0, reverse=True)


def _looks_poisoned(files: list[dict]) -> dict | None:
    """Heuristic poisoned-swarm check on a downloader file list.

    Returns a finding dict when the file list looks like malware, else None.
    """
    if not files:
        return None
    suspicious = [
        f for f in files if str(f.get("name", "")).lower().endswith(_SUSPICIOUS_EXTENSIONS)
    ]
    if suspicious:
        return {
            "verdict": "poisoned-swarm",
            "reason": "suspicious file extension",
            "files": [f.get("name") for f in suspicious],
        }
    videos = [f for f in files if str(f.get("name", "")).lower().endswith(_VIDEO_EXTENSIONS)]
    if not videos and len(files) == 1:
        return {
            "verdict": "poisoned-swarm",
            "reason": "single non-video file",
            "files": [files[0].get("name")],
        }
    return None


def _encode_family(release_title: str) -> str:
    """Bucket a release title into an encode family (group + codec + source)."""
    t = release_title.lower()
    codec = (
        "h265"
        if re.search(r"x265|h265|hevc", t)
        else "h264"
        if re.search(r"x264|h264|avc", t)
        else "?"
    )
    src = (
        "web"
        if "web" in t
        else "bluray"
        if "bluray" in t or "bdrip" in t
        else "hdtv"
        if "hdtv" in t
        else "?"
    )
    m = re.search(r"-([a-z0-9]+)$", t.strip())
    grp = m.group(1) if m else "?"
    return f"{grp}/{src}/{codec}"


def register_playbook_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register the composite playbook tools on the given Agent."""

    @agent.tool
    async def listenarr_fill_missing(
        ctx: RunContext[AgentDeps], limit: int = 10, grab: bool = False
    ) -> str:
        """Find monitored Listenarr books with no file and get them downloading.

        Listenarr's own search asks its indexers for the full Audible title plus
        the author, which returns nothing for titles carrying an edition
        parenthetical, so those books sit missing forever. This retries each one
        with progressively looser queries, discards clips too small to be an
        audiobook, and reports the best release per book.

        Read-only by default: pass grab=True to actually send them to the
        download client.
        """

        async def body() -> dict[str, Any]:
            report: dict[str, Any] = {"checked": 0, "grabbed": 0, "items": []}
            if grab:
                ctx.deps.require_write("listenarr_fill_missing")

            async with ctx.deps.listenarr() as client:
                books = await client.get_all_items()
                missing = [
                    b
                    for b in books
                    if b.get("monitored") and (b.get("status") or "") in ("no-file", "missing")
                ][:limit]
                report["checked"] = len(missing)

                for book in missing:
                    title = book.get("title") or ""
                    authors = book.get("authors") or []
                    author = authors[0] if authors else (book.get("author") or "")
                    item: dict[str, Any] = {"id": book.get("id"), "title": title, "tried": []}

                    best = None
                    for query in _search_queries(title, author):
                        results = await client.search(query, limit=50)
                        ranked = _rank_releases(results, title)
                        item["tried"].append(
                            {"query": query, "results": len(results), "usable": len(ranked)}
                        )
                        if ranked:
                            best = ranked[0]
                            break

                    if not best:
                        item["outcome"] = "no-release-found"
                        report["items"].append(item)
                        continue

                    item["release"] = {
                        "title": best.get("title"),
                        "sizeMB": round((best.get("size") or 0) / 1_000_000),
                        "seeders": best.get("seeders"),
                        "indexer": best.get("indexer"),
                    }
                    if not grab:
                        item["outcome"] = "candidate"
                    else:
                        await client.grab_release(
                            best["downloadReference"], audiobook_id=book.get("id")
                        )
                        item["outcome"] = "sent-to-download-client"
                        report["grabbed"] += 1
                    report["items"].append(item)

            return report

        try:
            return _wrap(await body())
        except PermissionError as e:
            return _wrap({"error": "permission-denied", "detail": str(e)})
        except ValueError as e:
            return _wrap({"error": "not-configured", "detail": str(e)})
        except (httpx.HTTPError, KeyError, AttributeError, TypeError) as e:
            logger.warning("listenarr_fill_missing failed: %s", e)
            return _wrap({"error": "playbook-failed", "detail": str(e)[:200]})

    @agent.tool
    async def diagnose_failed_grabs(
        ctx: RunContext[AgentDeps], media_type: str, item_id: int, episode_id: int = 0
    ) -> str:
        """Diagnose repeated grab failures for a movie or episode end-to-end.

        Runs: history -> failure classification (external strikes vs import
        errors vs stalls) -> Cleanuparr cross-check -> downloader file-list
        malware check -> interactive search bucketed by encode family ->
        recommendation. Read-only; any fix (delete, push release) is a
        separate tool call the caller must approve.
        """

        async def body() -> dict[str, Any]:
            finding: dict[str, Any] = {
                "media_type": media_type,
                "item_id": item_id,
                "episode_id": episode_id,
            }

            # 1. History
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    hist = await c.get_episode_history(episode_id) if episode_id else None
                    if hist is None:
                        raise ValueError("episode_id is required for TV diagnosis")
                    records = hist.get("records", [])
            elif media_type == "movie":
                async with ctx.deps.radarr() as c:
                    records = (await c.get_movie_history(item_id)).get("records", [])
            else:
                raise ValueError(f"unsupported media_type: {media_type}")

            failures = [
                r
                for r in records
                if r.get("eventType") in ("downloadFailed", "downloadImportFailed")
            ]
            grabs = [r for r in records if r.get("eventType") == "grabbed"]
            download_ids = {r.get("downloadId") for r in failures if r.get("downloadId")}
            finding["stats"] = {
                "grabs": len(grabs),
                "failures": len(failures),
                "failureMessages": sorted({r.get("message") for r in failures if r.get("message")}),
            }

            # 2. Classify
            external_strikes = any(
                any(
                    m.lower().startswith("manually") or m in _MANUAL_FAIL_MARKERS
                    for m in [r.get("message", "")]
                )
                for r in failures
            )
            import_errors = any(r.get("eventType") == "downloadImportFailed" for r in failures)
            finding["classification"] = {
                "externalStrikeSuspected": external_strikes,
                "importErrors": import_errors,
                "meaning": (
                    "external actor (Cleanuparr or a person) marked these failed"
                    if external_strikes
                    else "stalls/timeouts or indexer-side failures"
                ),
            }

            # 3. Cleanuparr cross-check
            if external_strikes:
                async with ctx.deps.cleanuparr() as cc:
                    try:
                        events = await cc.get_events(page_size=100)
                        strikes = [e for e in events if any(str(d) in str(e) for d in download_ids)]
                        finding["cleanuparr"] = (
                            {
                                "matchedStrikes": len(strikes),
                                "sample": strikes[:5],
                            }
                            if strikes
                            else {"matchedStrikes": 0}
                        )
                    except (httpx.HTTPError, KeyError, ValueError) as e:
                        finding["cleanuparr"] = {"error": str(e)[:150]}

            # 4. Downloader file-list check on any live torrent
            if settings.qbittorrent_url:
                async with ctx.deps.qbittorrent() as q:
                    torrents = await q.get_torrents()
                    live = [
                        t
                        for t in torrents
                        if t.get("hash") in download_ids
                        or any(str(d) in (t.get("name") or "") for d in download_ids)
                    ]
                    for t in live[:3]:
                        files = await q.get_item_files(t["hash"])
                        poisoned = _looks_poisoned(files)
                        if poisoned:
                            finding["malwareCheck"] = {
                                "torrent": t.get("name"),
                                "hash": t.get("hash"),
                                **poisoned,
                            }
                            break
                    else:
                        finding["malwareCheck"] = (
                            {"liveTorrentsChecked": len(live), "verdict": "clean"}
                            if live
                            else {"liveTorrentsChecked": 0}
                        )

            # 5. Interactive search, bucketed by encode family
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    releases = await c.interactive_search(item_id)
            else:
                async with ctx.deps.sonarr() as c:
                    releases = await c.interactive_search_episode(episode_id)

            families: dict[str, list[dict]] = {}
            for r in releases:
                rej = r.get("rejections") or []
                entry = {
                    "guid": r.get("guid"),
                    "title": r.get("title"),
                    "indexer": r.get("indexer"),
                    "seeders": r.get("seeders"),
                    "rejected": bool(rej),
                    "rejections": rej,
                }
                families.setdefault(_encode_family(r.get("title", "")), []).append(entry)

            blocklisted_families = [
                fam for fam, rel in families.items() if rel and all(x["rejected"] for x in rel)
            ]
            open_families = {
                fam: rel[:3] for fam, rel in families.items() if any(not x["rejected"] for x in rel)
            }
            finding["search"] = {
                "totalReleases": len(releases),
                "encodeFamilies": len(families),
                "fullyBlocklistedFamilies": blocklisted_families,
                "candidatesByFamily": open_families,
            }

            # 6. Recommendation
            if finding.get("malwareCheck", {}).get("verdict") == "poisoned-swarm":
                finding["recommendation"] = (
                    "Delete the live torrent (download_action delete with delete_files), "
                    "blocklist it, then push a release from a DIFFERENT encode family — "
                    "the same fake propagates to every indexer under this title."
                )
            elif open_families:
                finding["recommendation"] = (
                    "Push a release from an un-blocklisted encode family; prefer a "
                    "different group/source/codec, not just a different indexer."
                )
            elif releases:
                finding["recommendation"] = (
                    "Every family is blocklisted for this title; nothing healthy is "
                    "currently available. Wait or widen quality profiles."
                )
            else:
                finding["recommendation"] = "No releases found by any indexer."
            return finding

        try:
            return _wrap(await body())
        except ValueError as e:
            return _wrap({"error": "bad-arguments", "detail": str(e)})
        except (httpx.HTTPError, KeyError, AttributeError, TypeError) as e:
            logger.warning("diagnose_failed_grabs failed: %s", e)
            return _wrap({"error": "playbook-failed", "detail": str(e)[:200]})

    @agent.tool
    async def audit_downloads(ctx: RunContext[AgentDeps]) -> str:
        """Cross-reference every qBittorrent item against the arr queues.

        Flags: uncategorized items nothing manages, completed items that never
        imported, stalled items with zero seeds, and items whose file list
        contains blocked extensions. Answers 'my downloads folder is huge and
        I don't know why'. Read-only.
        """

        async def body() -> dict[str, Any]:
            if not settings.qbittorrent_url:
                raise ValueError("qBittorrent is not configured")

            managed_hashes: set[str] = set()
            queue_titles: list[str] = []
            if settings.sonarr_url and settings.sonarr_api_key:
                async with ctx.deps.sonarr() as c:
                    records = (await c.get_queue(page_size=200)).get("records", [])
                    for r in records:
                        if r.get("downloadId"):
                            managed_hashes.add(r["downloadId"].lower())
                        t = r.get("title")
                        if t:
                            queue_titles.append(t)
            if settings.radarr_url and settings.radarr_api_key:
                async with ctx.deps.radarr() as c:
                    records = (await c.get_queue(page_size=200)).get("records", [])
                    for r in records:
                        if r.get("downloadId"):
                            managed_hashes.add(r["downloadId"].lower())
                        t = r.get("title")
                        if t:
                            queue_titles.append(t)

            async with ctx.deps.qbittorrent() as q:
                torrents = await q.get_torrents()

            findings: list[dict] = []
            for t in torrents:
                h = (t.get("hash") or "").lower()
                state = t.get("state") or ""
                item: dict[str, Any] = {
                    "hash": h,
                    "name": t.get("name"),
                    "state": state,
                    "size": t.get("size"),
                    "flags": [],
                }
                if h not in managed_hashes and not any(
                    qt and qt in (t.get("name") or "") for qt in queue_titles
                ):
                    item["flags"].append("unmanaged")
                if state in ("stalledDL", "metaDL") and (t.get("num_seeds") or 0) == 0:
                    item["flags"].append("zero-seeds")
                findings.append(item)

            for item in findings[:10]:
                if item["flags"]:
                    async with ctx.deps.qbittorrent() as q:
                        files = await q.get_item_files(item["hash"])
                    poisoned = _looks_poisoned(files)
                    if poisoned:
                        item["flags"].append("poisoned")
                        item["malware"] = poisoned

            flagged = [f for f in findings if f["flags"]]
            return {
                "totalTorrents": len(findings),
                "flagged": len(flagged),
                "items": flagged,
                "note": (
                    "unmanaged = no arr queue entry owns it; zero-seeds = stalled with no peers"
                ),
            }

        try:
            return _wrap(await body())
        except ValueError as e:
            return _wrap({"error": "not-configured", "detail": str(e)})
        except (httpx.HTTPError, KeyError, AttributeError, TypeError) as e:
            logger.warning("audit_downloads failed: %s", e)
            return _wrap({"error": "playbook-failed", "detail": str(e)[:200]})
