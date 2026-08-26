#!/usr/bin/env python3
"""Premier League sleep-screen wallpaper for the Xteink X4 Pro.

Renders the current gameweek — completed scores and upcoming kickoffs — as a
480x800 1-bit BMP, the size and format CrossPoint's custom sleep screen wants.

Data comes from the same no-key endpoint premierleague.com's own site uses.
Club crests are fetched from the Premier League CDN and cached; the handful of
clubs that refuse (403) fall back to a drawn abbreviation badge.

Usage:  python3 generate_x4pro_pl.py [-o PremierLeague.bmp] [--tz Asia/Kolkata]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 480, 800
MARGIN = 14
CREST = 32
CREST_GAP = 20
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crests")

API_ROOT = "https://footballapi.pulselive.com/football"
API_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.premierleague.com",
    "User-Agent": "x4pro-premier-league/1.0",
}
CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "https://www.premierleague.com/",
}

FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[kind]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def api_get(path: str, **params) -> dict:
    url = f"{API_ROOT}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=API_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_badge_ids(season_id: int) -> dict[int, str]:
    """Map fixture team id -> crest id.

    The badge CDN is keyed by Opta id ("t3" = Arsenal), which is unrelated to
    the team id fixtures use (Arsenal is 1 there). Using the wrong one silently
    serves another club's crest, so always go through altIds.
    """
    payload = api_get("teams", comp=1, compSeasons=season_id, page=0, pageSize=100, altIds="true")
    ids = {}
    for team in payload.get("content", []):
        opta = (team.get("altIds") or {}).get("opta")
        if opta:
            ids[int(team["id"])] = opta
    return ids


def fetch_gameweek(tz: ZoneInfo) -> dict:
    """Current gameweek: the one in progress, else the next one due."""
    season = api_get("competitions/1/compseasons", page=0, pageSize=10)["content"][0]
    season_id = int(season["id"])
    badge_ids = fetch_badge_ids(season_id)
    weeks = api_get(f"compseasons/{season_id}/gameweeks", page=0, pageSize=100).get("gameweeks", [])
    active = [w for w in weeks if w.get("status") == "I"] or [w for w in weeks if w.get("status") == "U"]
    if not active:
        active = weeks
    target = int(active[0]["gameweek"])

    payload = api_get("fixtures", comp=1, compSeasons=season_id, page=0, pageSize=500, sort="asc")
    fixtures = [f for f in payload.get("content", []) if int(f.get("gameweek", {}).get("gameweek", -1)) == target]
    fixtures.sort(key=lambda f: float(f["kickoff"]["millis"]))

    matches = []
    for f in fixtures:
        home, away = f["teams"][0], f["teams"][1]

        def side(entry):
            team = entry["team"]
            club = team.get("club", {})
            return {
                "id": int(team["id"]),
                "badge": badge_ids.get(int(team["id"])),
                "name": team.get("shortName") or team.get("name") or "Unknown",
                "abbr": (club.get("abbr") or team.get("shortName") or "UNK")[:3].upper(),
                "score": entry.get("score"),
            }

        kickoff = datetime.fromtimestamp(float(f["kickoff"]["millis"]) / 1000, timezone.utc).astimezone(tz)
        matches.append({"home": side(home), "away": side(away), "kickoff": kickoff, "status": f.get("status", "U")})

    label = season.get("label", "")
    season_text = label.split("Season")[-1].strip() if "Season" in label else label
    return {
        "season": season_text,
        "gameweek": target,
        "matches": matches,
        "standings": fetch_standings(season_id),
    }


def fetch_standings(season_id: int) -> list[dict]:
    """League table, already carrying each club's crest id."""
    payload = api_get("standings", compSeasons=season_id, altIds="true", detail=2)
    tables = payload.get("tables", [])
    if not tables:
        return []
    rows = []
    for entry in tables[0].get("entries", []):
        team = entry["team"]
        overall = entry["overall"]
        rows.append({
            "pos": int(entry["position"]),
            "name": team.get("shortName") or team.get("name"),
            "badge": (team.get("altIds") or {}).get("opta"),
            "played": int(overall["played"]),
            "gd": int(overall["goalsDifference"]),
            "points": int(overall["points"]),
        })
    return rows


def crest(badge_id: str | None, size: int = CREST) -> Image.Image | None:
    """1-bit crest at `size` px, cached on disk. None when the CDN refuses."""
    if not badge_id:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{badge_id}.png")
    if not os.path.exists(path):
        for size in (50, 25, 100):
            url = f"https://resources.premierleague.com/premierleague/badges/{size}/{badge_id}.png"
            try:
                data = urllib.request.urlopen(urllib.request.Request(url, headers=CDN_HEADERS), timeout=20).read()
                Image.open(io.BytesIO(data)).save(path)
                break
            except (urllib.error.URLError, OSError):
                continue
        else:
            return None

    art = Image.open(path).convert("RGBA")
    flat = Image.alpha_composite(Image.new("RGBA", art.size, (255, 255, 255, 255)), art).convert("L")
    # Normalise per crest before thresholding: pale ones (Aston Villa's lion)
    # otherwise collapse to an empty shield outline.
    flat = ImageOps.autocontrast(flat, cutoff=2)
    flat = flat.resize((size, size), Image.LANCZOS)
    # Hard threshold, no dithering: dithered crests read as noise on e-ink.
    return flat.point(lambda v: 0 if v < 145 else 255, mode="L")


def draw_badge(draw: ImageDraw.ImageDraw, x: int, y: int, abbr: str, font) -> None:
    """Fallback for clubs whose crest the CDN won't serve."""
    draw.rounded_rectangle([x, y, x + CREST - 1, y + CREST - 1], radius=6, outline=0, width=2)
    w = draw.textlength(abbr, font=font)
    draw.text((x + (CREST - w) / 2, y + CREST / 2 - 7), abbr, font=font, fill=0)


def truncate(draw: ImageDraw.ImageDraw, text: str, font, limit: int) -> str:
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return text + "…"


TABLE_CREST = 22


def draw_table(img, d, y: int, bottom: int, rows: list[dict], fonts: dict, with_crests: bool) -> None:
    """Fill the space under the fixtures with as much of the table as fits."""
    row_h = 26 if with_crests else 23
    header_h = 22 + 12
    capacity = (bottom - y - header_h) // row_h
    if not rows or capacity < 4:
        return
    shown = rows[: min(capacity, len(rows))]

    col_pts = WIDTH - MARGIN - 10
    col_gd = col_pts - 44
    col_p = col_gd - 40

    d.rectangle([MARGIN, y, WIDTH - MARGIN, y + 22], fill=0)
    d.text((MARGIN + 8, y + 3), "TABLE", font=fonts["section"], fill=255)
    for label, x in (("P", col_p), ("GD", col_gd), ("PTS", col_pts)):
        w = d.textlength(label, font=fonts["small"])
        d.text((x - w, y + 5), label, font=fonts["small"], fill=255)
    y += 30

    for r in shown:
        pos = str(r["pos"])
        pw = d.textlength(pos, font=fonts["small"])
        d.text((MARGIN + 20 - pw, y + 4), pos, font=fonts["small"], fill=0)

        x = MARGIN + 28
        if with_crests:
            art = crest(r["badge"], TABLE_CREST)
            if art is not None:
                img.paste(art, (x, y + (row_h - TABLE_CREST) // 2 - 2))
            x += TABLE_CREST + 8

        name = truncate(d, r["name"], fonts["row"], col_p - x - 14)
        d.text((x, y + 3), name, font=fonts["row"], fill=0)

        for value, col, font in ((str(r["played"]), col_p, fonts["small"]),
                                 (f"{r['gd']:+d}", col_gd, fonts["small"]),
                                 (str(r["points"]), col_pts, fonts["row"])):
            w = d.textlength(value, font=font)
            d.text((col - w, y + 4), value, font=font, fill=0)

        y += row_h
        if r is not shown[-1]:
            d.line([MARGIN + 4, y - 1, WIDTH - MARGIN - 4, y - 1], fill=0, width=1)


def render(data: dict, tz: ZoneInfo, with_crests: bool = False) -> Image.Image:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    d = ImageDraw.Draw(img)

    f_title = load_font("bold", 30)
    f_sub = load_font("regular", 15)
    f_section = load_font("bold", 15)
    f_team = load_font("bold", 17)
    f_score = load_font("bold", 20)
    f_time = load_font("regular", 13)
    f_foot = load_font("regular", 12)
    f_abbr = load_font("bold", 13)

    # Title bar
    d.rectangle([0, 0, WIDTH, 52], fill=0)
    tw = d.textlength("PREMIER LEAGUE", font=f_title)
    d.text(((WIDTH - tw) / 2, 11), "PREMIER LEAGUE", font=f_title, fill=255)

    sub = f"{data['season']}  ·  GAMEWEEK {data['gameweek']}"
    sw = d.textlength(sub, font=f_sub)
    d.text(((WIDTH - sw) / 2, 60), sub, font=f_sub, fill=0)

    done = [m for m in data["matches"] if m["home"]["score"] is not None]
    todo = [m for m in data["matches"] if m["home"]["score"] is None]

    y = 86
    for heading, group in (("COMPLETED", done), ("UPCOMING", todo)):
        if not group:
            continue
        d.rectangle([MARGIN, y, WIDTH - MARGIN, y + 22], fill=0)
        d.text((MARGIN + 8, y + 3), heading, font=f_section, fill=255)
        count = f"{len(group)}"
        cw = d.textlength(count, font=f_section)
        d.text((WIDTH - MARGIN - 8 - cw, y + 3), count, font=f_section, fill=255)
        y += 30

        for m in group:
            row_h = CREST + 12
            cx = MARGIN + 2
            # Home crest, then away crest at the right edge.
            for side, x in ((m["home"], cx), (m["away"], WIDTH - MARGIN - 2 - CREST)):
                art = crest(side["badge"])
                if art is not None:
                    img.paste(art, (x, y))
                else:
                    draw_badge(d, x, y, side["abbr"], f_abbr)

            mid_y = y + CREST / 2 - 9
            if m["home"]["score"] is not None:
                centre = f"{m['home']['score']}–{m['away']['score']}"
                font_c = f_score
            else:
                centre = m["kickoff"].strftime("%a %H:%M").upper()
                font_c = f_time
            cwid = d.textlength(centre, font=font_c)
            d.text(((WIDTH - cwid) / 2, (mid_y + 4) if font_c is f_score else mid_y + 7), centre, font=font_c, fill=0)

            # Names fill the gap between crest and centre column.
            name_room = int((WIDTH / 2 - cwid / 2) - (MARGIN + CREST + CREST_GAP)) - 8
            home_name = truncate(d, m["home"]["name"], f_team, name_room)
            away_name = truncate(d, m["away"]["name"], f_team, name_room)
            d.text((MARGIN + CREST + CREST_GAP, mid_y + 6), home_name, font=f_team, fill=0)
            aw = d.textlength(away_name, font=f_team)
            d.text((WIDTH - MARGIN - CREST - CREST_GAP - aw, mid_y + 6), away_name, font=f_team, fill=0)

            y += row_h
            d.line([MARGIN + 4, y - 6, WIDTH - MARGIN - 4, y - 6], fill=0, width=1)
        y += 8

    if data.get("standings"):
        fonts = {"section": f_section, "small": f_time, "row": load_font("bold", 15)}
        draw_table(img, d, y, HEIGHT - 34, data["standings"], fonts, with_crests)

    stamp = datetime.now(tz).strftime("UPDATED %a %d %b · %H:%M").upper()
    fw = d.textlength(stamp, font=f_foot)
    d.text(((WIDTH - fw) / 2, HEIGHT - 26), stamp, font=f_foot, fill=0)

    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="PremierLeague.bmp")
    ap.add_argument("--tz", default="Asia/Kolkata")
    ap.add_argument("--preview", action="store_true", help="also write a PNG preview")
    ap.add_argument("--table-crests", action="store_true", help="draw crests in the league table too")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    data = fetch_gameweek(tz)
    img = render(data, tz, with_crests=args.table_crests)

    # 1-bit BMP: ~48KB instead of ~1.1MB for 24-bit, and CrossPoint's renderer
    # has a dedicated fast path for 1bpp bitmaps.
    img.convert("1").save(args.out, "BMP")
    if args.preview:
        img.save(os.path.splitext(args.out)[0] + ".png")
    print(f"wrote {args.out} ({os.path.getsize(args.out)} bytes) — gameweek {data['gameweek']}, "
          f"{len(data['matches'])} fixtures")


if __name__ == "__main__":
    main()
