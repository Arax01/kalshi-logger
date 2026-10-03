"""Sorts markets into groups for the reports.

Kalshi does not label "player prop" directly, so this uses the series ticker naming that Kalshi
uses consistently: KX + league + suffix, e.g. KXNFLGAME (game winner), KXNBAPTS (player points),
KXMLBHR (player home runs). Anything unrecognised falls back to a broad group, never dropped.
"""
import re

# League code in the series ticker -> (sport, league label). Longest codes are matched first.
LEAGUES = {
    "NFL": ("Football", "NFL"), "NCAAF": ("Football", "College Football"), "CFL": ("Football", "CFL"),
    "UFL": ("Football", "UFL"),
    "NBA": ("Basketball", "NBA"), "WNBA": ("Basketball", "WNBA"),
    "NCAAMB": ("Basketball", "College Basketball (M)"), "NCAAWB": ("Basketball", "College Basketball (W)"),
    "NCAAB": ("Basketball", "College Basketball"),
    "MLB": ("Baseball", "MLB"), "KBO": ("Baseball", "KBO"), "NPB": ("Baseball", "NPB"),
    "NHL": ("Hockey", "NHL"),
}
_LEAGUE_RE = re.compile(r"^KX(" + "|".join(sorted(LEAGUES, key=len, reverse=True)) + r")(.*)$")

# Per-game, single-player stat markets: suffix -> stat type.
PLAYER_PROP_STATS = {
    # Football
    "PASSYDS": "passing_yards", "PASSTDS": "passing_tds", "PASSATT": "pass_attempts",
    "PASSCOMP": "pass_completions", "PASSINT": "interceptions_thrown", "RSHYDS": "rushing_yards",
    "RSHATT": "rush_attempts", "RECYDS": "receiving_yards", "REC": "receptions",
    "RRYDS": "rush_plus_receiving_yards", "ANYTD": "anytime_td", "2TD": "two_plus_tds",
    "FIRSTTD": "first_td_scorer", "TD": "anytime_td", "TEAMFIRSTTD": "team_first_td_scorer", "TKL": "tackles", "SACK": "sacks", "INT": "interceptions",
    "LONGREC": "longest_reception", "LONGRSH": "longest_rush", "FFPTS": "fantasy_points",
    "LADDERREC": "receptions", "LADDERRECYDS": "receiving_yards", "LADDERRSHYDS": "rushing_yards",
    "ESCALATORREC": "receptions", "ESCALATORRECYDS": "receiving_yards",
    "ESCALATORRSHYDS": "rushing_yards", "FFPTSLADDER": "fantasy_points",
    "PASSYDSH2H": "passing_yards_h2h", "RECYDSH2H": "receiving_yards_h2h",
    "RSHYDSH2H": "rushing_yards_h2h",
    # Basketball
    "PTS": "points", "REB": "rebounds", "AST": "assists", "3PT": "threes", "BLK": "blocks",
    "STL": "steals", "PRA": "pts_reb_ast", "PR": "pts_reb", "PA": "pts_ast", "RA": "reb_ast",
    "FTM": "free_throws", "2D": "double_double", "3D": "triple_double",
    "FIRSTBASKET": "first_basket", "H2HPTS": "points_h2h", "H2HPRA": "pts_reb_ast_h2h",
    "H2H3PT": "threes_h2h",
    # Baseball
    "HR": "home_runs", "HIT": "hits", "KS": "strikeouts", "TB": "total_bases", "RBI": "rbis",
    "HRR": "hits_runs_rbis", "SB": "stolen_bases", "OUTS": "pitcher_outs", "HA": "hits_allowed",
    "WA": "walks_allowed", "WALK": "walks",
    # Hockey
    "GOAL": "goals", "SAVES": "saves", "SAVE": "saves", "FIRSTGOAL": "first_goal_scorer",
}
# NHL reuses PTS/AST for skater points/assists, which the shared map already covers.

_GAME_LINE_RE = re.compile(
    r"^(SPREAD|TOTAL|TEAMTOTAL|OT|OVERTIME|WINMARGIN|"
    r"[1-4](H|Q|P)(SPREAD|TOTAL|TEAMTOTAL|WINNER|FT|BTTS)?|F[357](SPREAD|TOTAL)?|"
    r"[12]H(SPREAD|TOTAL|TEAMTOTAL|WINNER|FT)?)$"
)

SPORT_TAGS = {
    "Soccer", "Basketball", "Football", "Baseball", "Tennis", "Golf", "Esports", "Hockey",
    "Motorsport", "Cricket", "MMA", "Boxing", "Rugby", "Table Tennis", "Lacrosse", "Cycling",
    "Volleyball", "Darts", "Chess",
}


def series_from_event(event_ticker):
    return (event_ticker or "").split("-")[0]


def classify(series_ticker, category, tags, is_combo=False):
    """Return dict(market_group, sport, league, stat_type)."""
    out = {"market_group": "other", "sport": None, "league": None, "stat_type": None}
    if is_combo:
        out["market_group"] = "combo"
        return out
    if category == "Crypto":
        out["market_group"] = "crypto"
    if category != "Sports":
        return out
    out["market_group"] = "sports_other"
    sport_tags = [t for t in (tags or []) if t in SPORT_TAGS]
    out["sport"] = sport_tags[0] if sport_tags else "Other"
    m = _LEAGUE_RE.match(series_ticker or "")
    if m:
        code, suffix = m.group(1), m.group(2)
        out["sport"], out["league"] = LEAGUES[code]
    else:
        # Other leagues follow the same naming, e.g. KXEPLGAME, KXKBOGAME.
        suffix = None
        g = re.match(r"^KX([A-Z0-9]+?)(GAME|MATCH)$", series_ticker or "")
        if g:
            out["league"] = g.group(1)
            out["market_group"] = "game_winner"
        return out
    if suffix in ("GAME", "MATCH"):
        out["market_group"] = "game_winner"
    elif suffix in PLAYER_PROP_STATS:
        out["market_group"] = "player_prop"
        out["stat_type"] = PLAYER_PROP_STATS[suffix]
    elif _GAME_LINE_RE.match(suffix):
        out["market_group"] = "game_line"
    return out
