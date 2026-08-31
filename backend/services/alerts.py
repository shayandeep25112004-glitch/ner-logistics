"""Alert evaluation, cooldown management, and multilingual translation services.

Supports 6 languages: English (en), Hindi (hi), Assamese (as), Bengali (bn),
Nepali (ne), and Manipuri/Meitei (mni).
"""

from __future__ import annotations

import datetime
from typing import Sequence

from config import ALERT_COOLDOWN_MINUTES, LANGUAGES, RISK_AT_RISK, RISK_BLOCKED
from db import db

TEMPLATES = {
    "high_risk_corridor": {
        "en": {
            "title": "Road blocked: {road}",
            "body": "{road} in {state} is assessed as blocked (risk {pct}%). Use the suggested alternate route. {count_str}",
        },
        "hi": {
            "title": "सड़क अवरुद्ध: {road}",
            "body": "{state} में {road} के अवरुद्ध होने का उच्च जोखिम ({pct}%) है। कृपया वैकल्पिक मार्ग का उपयोग करें। {count_str}",
        },
        "as": {
            "title": "পথ বন্ধ: {road}",
            "body": "{state}ত {road} অবরুদ্ধ হোৱাৰ সম্ভাৱনা (ঝুঁকি {pct}%)। বিকল্প পথ ব্যৱহাৰ কৰক। {count_str}",
        },
        "bn": {
            "title": "সড়ক অবরুদ্ধ: {road}",
            "body": "{state}-এ {road} অবরুদ্ধ হওয়ার উচ্চ ঝুঁকি ({pct}%)। বিকল্প পথ ব্যবহার করুন। {count_str}",
        },
        "ne": {
            "title": "सडक अवरुद्ध: {road}",
            "body": "{state} मा {road} अवरुद्ध हुने उच्च जोखिम ({pct}%) छ। वैकल्पिक मार्ग प्रयोग गर्नुहोस्। {count_str}",
        },
        "mni": {
            "title": "লম্বী থিংল্লে: {road}",
            "body": "{state} দা {road} থিংজিনবগী ওইথোকপা লৈ ({pct}%)। অতোপ্পা লম্বী শীজিন্নবীয়ু। {count_str}",
        },
    },
    "delay": {
        "en": {
            "title": "Significant delay: {road}",
            "body": "Slow movement and terrain disruption risk ({pct}%) on {road} in {state}.",
        },
        "hi": {
            "title": "भारी देरी: {road}",
            "body": "{state} में {road} पर भूस्खलन जोखिम ({pct}%) के कारण आवागमन धीमा है।",
        },
        "as": {
            "title": "যাতায়াতত পলম: {road}",
            "body": "{state}ত {road}ত ভূমিস্খলনৰ আশংকাৰ বাবে যাতায়াত লেহেমীয়া হৈছে।",
        },
        "bn": {
            "title": "চলাচলে বিলম্ব: {road}",
            "body": "{state}-এ {road}-এ ধসের ঝুঁকির কারণে চলাচল ধীরগতিতে হচ্ছে।",
        },
        "ne": {
            "title": "ढिलाइ हुने: {road}",
            "body": "{state} को {road} मा पहिरोको जोखिमले गर्दा यातायात सुस्त छ।",
        },
        "mni": {
            "title": "চৎ-থোক য়াম্না থেংগনি: {road}",
            "body": "{state} গী {road} তা লৈবাক চিংখায়বগী অকিবা লৈবনা চৎথোক-চৎশিন তপ্পা ওইগনি।",
        },
    },
}

_ALERT_COOLDOWNS: dict[str, datetime.datetime] = {}


def translate_alert(
    kind: str,
    lang: str,
    road: str,
    state: str,
    risk: float = 0.85,
    affected_count: int = 1,
) -> dict:
    """Format alert in the requested language."""
    if lang not in LANGUAGES:
        lang = "en"
    if kind not in TEMPLATES:
        kind = "high_risk_corridor"

    pct = f"{risk * 100:.0f}"
    count_str = f"{affected_count} segments on this corridor are affected." if affected_count > 1 else ""

    tmpl = TEMPLATES[kind].get(lang, TEMPLATES[kind]["en"])
    title = tmpl["title"].format(road=road, state=state, pct=pct, count_str=count_str)
    body = tmpl["body"].format(road=road, state=state, pct=pct, count_str=count_str)

    return {
        "language": lang,
        "kind": kind,
        "title": title,
        "body": body,
        "road": road,
        "state": state,
        "risk": risk,
    }


def evaluate_alerts(scored_edges: Sequence[dict]) -> list[dict]:
    """Evaluate scored edges and generate aggregated corridor alerts with cooldown."""
    now = datetime.datetime.now()
    cooldown_delta = datetime.timedelta(minutes=ALERT_COOLDOWN_MINUTES)

    # Clean old cooldowns
    expired = [k for k, t in _ALERT_COOLDOWNS.items() if now - t > cooldown_delta]
    for k in expired:
        del _ALERT_COOLDOWNS[k]

    # Group high risk edges by corridor (road + state)
    high_risk = [e for e in scored_edges if e.get("risk", 0.0) >= RISK_AT_RISK]
    corridors = {}
    for e in high_risk:
        road = e.get("road") or e.get("ref") or e.get("name") or "Highway"
        st = e.get("state") or "NER"
        key = (road, st)
        if key not in corridors:
            corridors[key] = []
        corridors[key].append(e)

    new_alerts = []
    for (road, st), edges in corridors.items():
        if len(new_alerts) >= 60: # Cap at 60 alerts per run
            break

        cd_key = f"{road}:{st}"
        if cd_key in _ALERT_COOLDOWNS:
            continue

        max_risk = max(e.get("risk", 0.0) for e in edges)
        sev = "critical" if max_risk >= RISK_BLOCKED else "high" if max_risk >= 0.50 else "moderate"
        
        tr = translate_alert("high_risk_corridor", "en", road, st, max_risk, len(edges))
        
        # Representative coordinate
        lat = edges[0].get("lat")
        lon = edges[0].get("lon")
        edge_id = edges[0].get("id")

        rec = {
            "kind": "high_risk_corridor",
            "severity": sev,
            "title": tr["title"],
            "body": tr["body"],
            "lat": lat,
            "lon": lon,
            "edge_id": edge_id,
            "district": st,
            "state": st,
            "risk": round(max_risk, 4),
            "channel": "dashboard",
            "language": "en",
            "created_at": now.isoformat(),
        }
        new_alerts.append(rec)
        _ALERT_COOLDOWNS[cd_key] = now

    if new_alerts:
        with db() as conn:
            conn.executemany(
                """INSERT INTO alert
                   (kind, severity, title, body, lat, lon, edge_id, district, state, risk, channel, language, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        a["kind"],
                        a["severity"],
                        a["title"],
                        a["body"],
                        a["lat"],
                        a["lon"],
                        a["edge_id"],
                        a["district"],
                        a["state"],
                        a["risk"],
                        a["channel"],
                        a["language"],
                        a["created_at"],
                    )
                    for a in new_alerts
                ],
            )

    return new_alerts
