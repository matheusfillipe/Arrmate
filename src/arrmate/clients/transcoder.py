"""H.265/HEVC transcoding client using ffmpeg.

Scans Sonarr/Radarr libraries for files not already encoded in H.265 and
transcodes them in the background, replacing originals on success.

Requires:
  - ffmpeg installed (included in Docker image)
  - Media directories mounted into the Arrmate container at the same paths
    that Sonarr/Radarr report (configure via volume mounts in compose).
"""

import asyncio
import logging
import shutil
import subprocess  # nosec B404
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from arrmate.config.settings import settings
from arrmate.core.models import MediaType

logger = logging.getLogger(__name__)

# Codec strings that Sonarr/Radarr report for H.265/HEVC files (already done)
_H265_CODECS = {"hevc", "x265", "h265", "h.265"}

_JOB_MAX_AGE = timedelta(hours=24)
_JOB_MAX_TOTAL = 100

JobStatus = Literal["pending", "running", "completed", "cancelled"]
_FINISHED: tuple[JobStatus, ...] = ("completed", "cancelled")


class _ArrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _MediaInfo(_ArrRecord):
    video_codec: str = Field(default="", alias="videoCodec")


class _MediaFile(_ArrRecord):
    path: str | None = None
    relative_path: str | None = Field(default=None, alias="relativePath")
    size: int = 0
    media_info: _MediaInfo = Field(default_factory=_MediaInfo, alias="mediaInfo")


class _Movie(_ArrRecord):
    title: str = "Unknown"
    movie_file: _MediaFile | None = Field(default=None, alias="movieFile")


class _Series(_ArrRecord):
    id: int
    title: str


_MOVIES = TypeAdapter(list[_Movie])
_SERIES = TypeAdapter(list[_Series])
_EPISODE_FILES = TypeAdapter(list[_MediaFile])


class TranscodeFile(BaseModel):
    title: str
    path: str
    codec: str
    media_type: MediaType
    size: int


class TranscodedFile(BaseModel):
    title: str
    original_size: str
    new_size: str
    saved: str


class TranscodeJob(BaseModel):
    id: str
    status: JobStatus = "pending"
    media_type: MediaType
    title: str | None
    total: int
    completed: int = 0
    failed: int = 0
    saved_bytes: int = 0
    current_file: str | None = None
    errors: list[str] = []
    completed_files: list[TranscodedFile] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    cancelled: bool = False


_jobs: dict[str, TranscodeJob] = {}


# ── Public job store helpers ───────────────────────────────────────────────────


def get_job(job_id: str) -> TranscodeJob | None:
    """Return a job by ID, or None if not found."""
    return _jobs.get(job_id)


def _prune_jobs() -> None:
    """Remove finished jobs older than 24h, then the oldest finished ones beyond 100 entries."""
    cutoff = datetime.now(UTC) - _JOB_MAX_AGE
    for job in list(_jobs.values()):
        if job.status in _FINISHED and job.created_at < cutoff:
            del _jobs[job.id]
    if len(_jobs) > _JOB_MAX_TOTAL:
        finished = sorted(
            (j for j in _jobs.values() if j.status in _FINISHED), key=lambda j: j.created_at
        )
        for job in finished[: len(_jobs) - _JOB_MAX_TOTAL]:
            del _jobs[job.id]


def get_all_jobs() -> list[TranscodeJob]:
    """Return all jobs, newest first."""
    _prune_jobs()
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


def cancel_job(job_id: str) -> bool:
    """Request cancellation of a running job. Returns True if job found."""
    job = _jobs.get(job_id)
    if job and job.status in ("pending", "running"):
        job.cancelled = True
        return True
    return False


# ── Codec helpers ──────────────────────────────────────────────────────────────


def _needs_transcode(media_file: _MediaFile) -> bool:
    codec = media_file.media_info.video_codec.strip().lower()
    return bool(media_file.path) and codec not in _H265_CODECS


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _transcode_file(title: str, media_file: _MediaFile, media_type: MediaType) -> TranscodeFile:
    return TranscodeFile(
        title=title,
        path=media_file.path or "",
        codec=media_file.media_info.video_codec or "unknown",
        media_type=media_type,
        size=media_file.size,
    )


# ── Library scanning ───────────────────────────────────────────────────────────


async def _get_radarr_files(title_filter: str | None) -> list[TranscodeFile]:
    """Fetch movie files from Radarr that are not yet H.265."""
    if not settings.radarr_url or not settings.radarr_api_key:
        return []

    base = settings.radarr_url.rstrip("/")
    headers = {"X-Api-Key": settings.radarr_api_key}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/api/v3/movie", headers=headers)
        resp.raise_for_status()
        movies = _MOVIES.validate_json(resp.content)

    return [
        _transcode_file(movie.title, movie.movie_file, MediaType.MOVIE)
        for movie in movies
        if movie.movie_file is not None
        and _needs_transcode(movie.movie_file)
        and (not title_filter or title_filter.lower() in movie.title.lower())
    ]


async def _get_sonarr_files(title_filter: str | None) -> list[TranscodeFile]:
    """Fetch episode files from Sonarr that are not yet H.265."""
    if not settings.sonarr_url or not settings.sonarr_api_key:
        return []

    base = settings.sonarr_url.rstrip("/")
    headers = {"X-Api-Key": settings.sonarr_api_key}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/api/v3/series", headers=headers)
        resp.raise_for_status()
        all_series = [
            s
            for s in _SERIES.validate_json(resp.content)
            if not title_filter or title_filter.lower() in s.title.lower()
        ]

        # Cap concurrent episode-file requests to avoid overwhelming Sonarr
        sem = asyncio.Semaphore(5)

        async def fetch_series_files(series: _Series) -> list[TranscodeFile]:
            async with sem:
                try:
                    r = await client.get(
                        f"{base}/api/v3/episodefile",
                        params={"seriesId": series.id},
                        headers=headers,
                    )
                    r.raise_for_status()
                    return [
                        _transcode_file(
                            f"{series.title} - {ef.relative_path or ef.path}", ef, MediaType.TV
                        )
                        for ef in _EPISODE_FILES.validate_json(r.content)
                        if _needs_transcode(ef)
                    ]
                except (httpx.HTTPError, ValueError) as e:
                    logger.warning("Failed to get files for %s: %s", series.title, e)
                    return []

        batches = await asyncio.gather(*[fetch_series_files(s) for s in all_series])

    return [f for batch in batches for f in batch]


async def scan_for_transcode(
    media_type: MediaType,
    title: str | None = None,
) -> list[TranscodeFile]:
    """Return library files that need H.265 transcoding, optionally filtered by title."""
    match media_type:
        case MediaType.MOVIE:
            return await _get_radarr_files(title)
        case MediaType.TV:
            return await _get_sonarr_files(title)
        case _:
            return []


# ── ffmpeg execution ───────────────────────────────────────────────────────────


def ffmpeg_available() -> bool:
    """Return True if ffmpeg is installed."""
    return shutil.which("ffmpeg") is not None


def _transcode_sync(file_path: str, crf: int, preset: str) -> tuple[bool, str]:
    """Blocking transcode of one file to H.265.

    Outputs to a .tmp.mkv sibling, then atomically renames over the original.
    Returns (success, error_message).
    """
    src = Path(file_path)
    if not src.exists():
        return False, f"File not found: {file_path}"

    tmp = src.with_suffix(".tmp.mkv")

    try:
        cmd = [
            "ffmpeg",
            "-i",
            str(src),
            "-c:v",
            "libx265",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-tag:v",
            "hvc1",
            "-y",
            str(tmp),
        ]
        proc = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=14400,  # 4-hour limit per file
        )
        if proc.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            stderr_tail = proc.stderr[-500:] if proc.stderr else "unknown error"
            return False, f"ffmpeg exited {proc.returncode}: {stderr_tail}"

        tmp.replace(src)
        return True, ""

    except subprocess.TimeoutExpired:
        if tmp.exists():
            tmp.unlink()
        return False, "Timed out after 4 hours"
    except (OSError, subprocess.SubprocessError) as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                logger.debug("temp transcode file already gone", exc_info=True)
        return False, str(exc)


def _transcode_sync_validated(
    file_path: str,
    crf: int,
    preset: str,
    allowed_roots: list[str],
) -> tuple[bool, str]:
    """Validate file_path is within allowed_roots, then delegate to _transcode_sync.

    If allowed_roots is empty, path validation is skipped for backward compatibility
    with deployments that have not configured TRANSCODE_ALLOWED_ROOTS.
    """
    if allowed_roots:
        resolved = Path(file_path).resolve()
        allowed = [Path(r).resolve() for r in allowed_roots]
        if not any(resolved.is_relative_to(root) for root in allowed):
            return False, f"path not within allowed media directory: {file_path}"
    return _transcode_sync(file_path, crf, preset)


# ── Background job runner ──────────────────────────────────────────────────────


async def run_transcode_job(job_id: str, files: list[TranscodeFile]) -> None:
    """Background coroutine: processes all files in a transcode job."""
    job = _jobs.get(job_id)
    if not job:
        return

    job.status = "running"
    loop = asyncio.get_event_loop()
    crf = settings.transcode_crf
    preset = settings.transcode_preset

    for file_info in files:
        if job.cancelled:
            break

        job.current_file = file_info.path

        try:
            allowed_roots = list(settings.transcode_allowed_roots)
            success, error = await loop.run_in_executor(
                None, _transcode_sync_validated, file_info.path, crf, preset, allowed_roots
            )

            if success:
                try:
                    new_size = Path(file_info.path).stat().st_size
                except OSError:
                    new_size = 0
                saved = max(0, file_info.size - new_size)
                job.completed += 1
                job.saved_bytes += saved
                job.completed_files.append(
                    TranscodedFile(
                        title=file_info.title,
                        original_size=_format_bytes(file_info.size),
                        new_size=_format_bytes(new_size),
                        saved=_format_bytes(saved),
                    )
                )
            else:
                job.failed += 1
                job.errors.append(f"{file_info.title}: {error}")

        except (OSError, subprocess.SubprocessError) as exc:
            job.failed += 1
            job.errors.append(f"{file_info.title}: {exc}")

    job.current_file = None
    job.status = "cancelled" if job.cancelled else "completed"
    job.finished_at = datetime.now(UTC)


# ── Job creation ───────────────────────────────────────────────────────────────


def create_job(
    files: list[TranscodeFile],
    media_type: MediaType,
    title: str | None = None,
) -> str:
    """Create a new transcode job entry and return its ID."""
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = TranscodeJob(id=job_id, media_type=media_type, title=title, total=len(files))
    return job_id
