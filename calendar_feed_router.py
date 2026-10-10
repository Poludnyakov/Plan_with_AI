"""Public endpoints of the private calendar link (no login: the token is the key)."""

import html
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from calendar_feed_service import build_feed, feed_links
from config import settings
from database import get_db


router = APIRouter(tags=["Calendar subscription"])
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


def _checked(token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail="Ссылка календаря не найдена")
    return token


@router.get("/calendar/feed/{token}.ics", include_in_schema=False)
async def calendar_feed(token: str, db: AsyncSession = Depends(get_db)):
    body = await build_feed(db, _checked(token))
    if body is None:
        raise HTTPException(status_code=404, detail="Ссылка календаря не найдена или сброшена")
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="planiruy.ics"',
            "Cache-Control": "private, max-age=300",
            "X-Robots-Tag": "noindex",
        },
    )


CONNECT_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Подключить календарь — планиИруй!</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,sans-serif;background:#f4f6fb;color:#172033}}
main{{max-width:460px;margin:0 auto;padding:28px 20px}}
h1{{font-size:22px;margin:0 0 6px}} p{{color:#4b5568;line-height:1.45}}
.hero{{display:block;width:132px;height:132px;margin:0 auto 6px}} h1,main>p{{text-align:center}}
a.button{{display:grid;grid-template-columns:44px 1fr;gap:2px 12px;align-items:center;margin:12px 0;padding:12px 16px;border-radius:14px;background:#fff;
color:#172033;text-decoration:none;font-weight:650;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
a.button img{{width:44px;height:44px;grid-row:span 2}}
a.button span{{display:block;font-weight:400;font-size:14px;color:#5f6878}}
.copy{{display:flex;gap:8px;margin-top:18px}} input{{flex:1;min-width:0;padding:12px;border:1px solid #cdd3df;border-radius:10px;font-size:14px}}
button{{padding:12px 14px;border:0;border-radius:10px;background:#2563d9;color:#fff;font-weight:650;font:inherit}}
@media (prefers-color-scheme:dark){{body{{background:#11151b;color:#eef1f6}} p,small{{color:#a9b0bd!important}}
a.button{{background:#191e26;color:#eef1f6}} a.button span{{color:#a9b0bd}} input{{background:#191e26;color:#eef1f6;border-color:#303743}}}}
small{{display:block;margin-top:18px;color:#5f6878;line-height:1.4}}
.hint{{margin:-4px 0 12px;padding:10px 12px;border-radius:12px;background:#e8f0ff;color:#17477f;font-size:14px;text-align:left}}
@media (prefers-color-scheme:dark){{.hint{{background:#203451;color:#dceaff!important}}}}
</style></head><body><main>
<img class="hero" src="/static/brand/emoji/calendar_sync.webp" alt="" width="132" height="132">
<h1>Подключить календарь</h1>
<p>События и занятия из планиИруй! появятся в вашем календаре и будут обновляться сами.</p>
<a class="button" href="{webcal}"><img src="/static/brand/pack/calendar.webp" alt="" width="44" height="44">Apple Календарь<span>iPhone, iPad, Mac — откроется окно подписки</span></a>
<a class="button" href="{google}" target="_blank" rel="noopener"><img src="/static/brand/pack/calendar_sync.webp" alt="" width="44" height="44">Google Календарь<span>Откроется «Добавить календарь по URL»</span></a>
<a class="button" id="yandex" href="https://calendar.yandex.ru/" target="_blank" rel="noopener"><img src="/static/brand/pack/pin.webp" alt="" width="44" height="44">Яндекс Календарь<span>Ссылка скопируется сама → «Добавить в мои календари» или «Подписаться» → вставьте</span></a>
<p class="hint" id="yandexHint" hidden>Ссылка скопирована. В Яндекс Календаре нажмите «Добавить в мои календари» или «Подписаться» и вставьте её в поле адреса.</p>
<div class="copy"><input id="link" value="{https}" readonly><button id="copy" type="button">Копировать</button></div>
<small>Apple обновляет подписку от 5 минут, Яндекс — примерно раз в час, Google — раз в
несколько часов. Ссылка личная: если поделились ею случайно, отправьте боту
/calendars new — старая перестанет работать.</small>
</main><script>
async function copyLink(){{const input=document.getElementById('link');
try{{await navigator.clipboard.writeText(input.value)}}catch(_){{input.select();document.execCommand('copy')}}}}
document.getElementById('copy').onclick=async()=>{{await copyLink();document.getElementById('copy').textContent='Скопировано';}};
// Yandex has no subscribe-by-link URL: copy the link, then its page opens.
document.getElementById('yandex').addEventListener('click',()=>{{copyLink();document.getElementById('yandexHint').hidden=false;}});
</script></body></html>"""


@router.get("/calendar/connect/{token}", response_class=HTMLResponse, include_in_schema=False)
async def calendar_connect(token: str):
    links = feed_links(_checked(token), settings.APP_URL)
    return HTMLResponse(
        CONNECT_PAGE.format(**{key: html.escape(value, quote=True) for key, value in links.items()}),
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )
