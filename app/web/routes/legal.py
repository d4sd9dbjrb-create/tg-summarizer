from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/tos", response_class=HTMLResponse)
async def tos(request: Request):
    return request.app.state.templates.TemplateResponse("tos.html", {"request": request})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return request.app.state.templates.TemplateResponse("privacy.html", {"request": request})
