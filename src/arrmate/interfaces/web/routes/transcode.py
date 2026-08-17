"""Web routes: transcode."""

from ._shared import (  # noqa: F401
    Depends,
    HTMLResponse,
    Request,
    _base_ctx,
    auth_router,
    cancel_job,
    get_all_jobs,
    require_power_user,
    router,
    templates,
)


@router.get("/transcode", response_class=HTMLResponse, dependencies=[Depends(require_power_user)])
async def transcode_page(request: Request):
    """Transcode job status page."""
    jobs = get_all_jobs()
    return templates.TemplateResponse(
        request,
        "pages/transcode.html",
        {**_base_ctx(request), "jobs": jobs},
    )


@router.get("/transcode/status", response_class=HTMLResponse)
async def transcode_status(request: Request):
    """HTMX partial: live job status panel (auto-refreshes while jobs are running)."""
    jobs = get_all_jobs()
    return templates.TemplateResponse(
        request,
        "partials/transcode_status.html",
        {"jobs": jobs},
    )


@router.post(
    "/transcode/cancel/{job_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def transcode_cancel(request: Request, job_id: str):
    """Cancel a running transcode job."""
    ok = cancel_job(job_id)
    jobs = get_all_jobs()
    return templates.TemplateResponse(
        request,
        "partials/transcode_status.html",
        {
            "jobs": jobs,
            "toast_message": f"Job {job_id} cancellation requested."
            if ok
            else f"Job {job_id} not found.",
            "toast_type": "success" if ok else "error",
        },
    )
