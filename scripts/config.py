"""Shared configuration for the Østjylland ro-index pipeline."""

# The 7 municipalities in Østjyllands Politi district.
KOMMUNER = ["Aarhus", "Randers", "Syddjurs", "Norddjurs", "Favrskov", "Odder", "Samsø"]

# DAWA kommunekoder -> name, used to resolve which kommune an address falls in
# (via api.dataforsyningen.dk/kommuner/reverse?x=&y=).
KOMKODE = {
    "0751": "Aarhus", "0730": "Randers", "0706": "Syddjurs",
    "0707": "Norddjurs", "0710": "Favrskov", "0727": "Odder", "0741": "Samsø",
}

# Severity weights per incident type. A burglary is not the same as a parking
# fine; the score is built on the weighted count, not the raw count.
SEVERITY = {
    "Indbrud": 3, "Vold": 5, "Narko": 2, "Tyveri": 2,
    "Færdsel": 1, "Hærværk": 2, "Spirituskørsel": 3, "Andet": 1,
}

# Baseline model
WINDOW_WEEKS = 8        # how far back the trailing baseline looks
MIN_SAMPLES = 3         # below this many same-weekday samples -> baseline immature
SCORE_SLOPE = 13        # score = 50 - SCORE_SLOPE * z, clamped to [2, 98]
OUT_DAYS = 56           # how many days of score history to publish per kommune
