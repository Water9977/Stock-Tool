import os
from datetime import datetime, time
from dotenv import load_dotenv

# Load variables from .env if it exists
load_dotenv()

class Config:
    CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
    CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
    REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

    MOBILE_NUMBER = os.getenv("UPSTOX_MOBILE_NUMBER")
    PIN = os.getenv("UPSTOX_PIN")
    TOTP_SECRET = os.getenv("UPSTOX_TOTP_SECRET")

    # Shared secret required (via X-API-Key header) to call mutating endpoints (/execute,
    # /admin/reset-simulation). If unset, those endpoints refuse all requests rather than
    # silently allowing unauthenticated order placement / DB wipes on a public URL.
    API_KEY = os.getenv("API_KEY")

    # Upstox mandates order placement/modification/cancellation requests originate from a
    # registered static IP (error UDAPI1154 otherwise) — cloud hosts like Railway rotate
    # outbound IPs by default. Set this to an HTTP(S) proxy URL from a static-IP provider
    # (e.g. QuotaGuard Static, a fixed-IP VPS) and register that proxy's IP in the Upstox
    # developer console. Left unset, order calls go out on Railway's normal rotating IP,
    # which WILL be rejected in LIVE mode once real orders are attempted — this is provider
    # account setup this codebase cannot provision on its own; None here is a valid no-op
    # (httpx.AsyncClient(proxy=None) behaves as if no proxy were configured at all).
    UPSTOX_HTTP_PROXY = os.getenv("UPSTOX_HTTP_PROXY")
    STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", 300000.0))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 200000.0))
    # Per-trade cap kept low so many concurrent positions can run across different symbols at
    # once, instead of 1-2 trades locking up the whole account for hours waiting on a target.
    MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", 80000.0))

    # Target/stop-loss percentages for new entries. Small on purpose — the point is fast,
    # frequent, small-profit trades that free up capital quickly for the next signal, rather
    # than a big target that can take all day (or never) to hit while capital sits idle.
    TARGET_PCT = float(os.getenv("TARGET_PCT", 0.01))
    SL_PCT = float(os.getenv("SL_PCT", 0.005))

    TRADING_MODE = os.getenv("TRADING_MODE", "VIRTUAL").upper()

    # OHLC candle aggregation period (seconds). Live NSE uses 300 (5-min candles).
    # For VIRTUAL end-to-end testing this can be lowered so indicators populate fast
    # enough to actually generate trades within a short simulation window.
    CANDLE_PERIOD_SECONDS = int(os.getenv("CANDLE_PERIOD_SECONDS", 300))

    # VIRTUAL-mode auto-flatten + suspend timer (hours).
    VIRTUAL_RUNTIME_HOURS = float(os.getenv("VIRTUAL_RUNTIME_HOURS", 5.0))

    # Nifty50 constituents individually tradeable within Rs.3500 per-trade cap (price <= cap
    # at time of writing). Instrument keys verified against Upstox's public instrument master
    # (assets.upstox.com/market-quote/instruments/exchange/complete.json.gz), not guessed.
    # Excluded (real Nifty50 members, but too expensive for the cap): MARUTI, ULTRACEMCO,
    # BAJAJ-AUTO, APOLLOHOSP, EICHERMOT, DIVISLAB, BRITANNIA, HEROMOTOCO, TITAN, LT.
    # Note: TATAMOTORS demerged in 2025 into passenger-vehicle (TMPV) and commercial-vehicle
    # (TMCV) entities; TMPV is used here. LTIMINDTREE could not be located in the instrument
    # master under any known trading symbol and was dropped rather than guessed.
    SYMBOL_MAP = {
        "WIPRO.NS":       "NSE_EQ|INE075A01022",  # ~Rs.170
        "TATASTEEL.NS":   "NSE_EQ|INE081A01020",  # ~Rs.187
        "ONGC.NS":        "NSE_EQ|INE213A01029",  # ~Rs.235
        "POWERGRID.NS":   "NSE_EQ|INE752E01010",  # ~Rs.287
        "ITC.NS":         "NSE_EQ|INE154A01025",  # ~Rs.290
        "BPCL.NS":        "NSE_EQ|INE029A01011",  # ~Rs.303
        "TATAMOTORS.NS":  "NSE_EQ|INE155A01022",  # ~Rs.349 (Tata Motors Passenger Vehicles)
        "NTPC.NS":        "NSE_EQ|INE733E01010",  # ~Rs.359
        "KOTAKBANK.NS":   "NSE_EQ|INE237A01036",  # ~Rs.397
        "COALINDIA.NS":   "NSE_EQ|INE522F01014",  # ~Rs.436
        "UPL.NS":         "NSE_EQ|INE628A01036",  # ~Rs.568
        "HDFCLIFE.NS":    "NSE_EQ|INE795G01014",  # ~Rs.569
        "HDFCBANK.NS":    "NSE_EQ|INE040A01034",  # ~Rs.801
        "HINDALCO.NS":    "NSE_EQ|INE038A01020",  # ~Rs.941
        "INDUSINDBK.NS":  "NSE_EQ|INE095A01012",  # ~Rs.944
        "INFY.NS":        "NSE_EQ|INE009A01021",  # ~Rs.997
        "BAJFINANCE.NS":  "NSE_EQ|INE296A01032",  # ~Rs.1023
        "SBIN.NS":        "NSE_EQ|INE062A01020",  # ~Rs.1038
        "SHRIRAMFIN.NS":  "NSE_EQ|INE721A01047",  # ~Rs.1051
        "HCLTECH.NS":     "NSE_EQ|INE860A01027",  # ~Rs.1055
        "TATACONSUM.NS":  "NSE_EQ|INE192A01025",  # ~Rs.1084
        "JSWSTEEL.NS":    "NSE_EQ|INE019A01038",  # ~Rs.1216
        "RELIANCE.NS":    "NSE_EQ|INE002A01018",  # ~Rs.1310
        "DRREDDY.NS":     "NSE_EQ|INE089A01031",  # ~Rs.1337
        "TECHM.NS":       "NSE_EQ|INE669C01036",  # ~Rs.1362
        "AXISBANK.NS":    "NSE_EQ|INE238A01034",  # ~Rs.1375
        "ICICIBANK.NS":   "NSE_EQ|INE090A01021",  # ~Rs.1382
        "NESTLEIND.NS":   "NSE_EQ|INE239A01024",  # ~Rs.1443
        "CIPLA.NS":       "NSE_EQ|INE059A01026",  # ~Rs.1448
        "SBILIFE.NS":     "NSE_EQ|INE123W01016",  # ~Rs.1773
        "BAJAJFINSV.NS":  "NSE_EQ|INE918I01026",  # ~Rs.1780
        "ADANIPORTS.NS":  "NSE_EQ|INE742F01042",  # ~Rs.1859
        "SUNPHARMA.NS":   "NSE_EQ|INE044A01036",  # ~Rs.1863
        "BHARTIARTL.NS":  "NSE_EQ|INE397D01024",  # ~Rs.1875
        "TCS.NS":         "NSE_EQ|INE467B01029",  # ~Rs.2033
        "HINDUNILVR.NS":  "NSE_EQ|INE030A01027",  # ~Rs.2164
        "ASIANPAINT.NS":  "NSE_EQ|INE021A01026",  # ~Rs.2711
        "ADANIENT.NS":    "NSE_EQ|INE423A01024",  # ~Rs.3106
        "M&M.NS":         "NSE_EQ|INE101A01026",  # ~Rs.3134
        "GRASIM.NS":      "NSE_EQ|INE047A01021",  # ~Rs.3143

        # --- Nifty Next 50 / Midcap 150 / Smallcap 250 additions (2026-07-06) ---
        # Constituent lists pulled live from NSE's public archives
        # (archives.nseindia.com/content/indices/ind_nifty{next50,midcap150,smallcap250}list.csv),
        # not from memory. All 450 candidates cross-referenced against Upstox's instrument
        # master by ISIN (same discipline as the original 40 — zero guessed instrument keys).
        # Selected top 160 by a combined score favoring high 1-month average daily
        # (High-Low)/Close range % (real intraday volatility, not a guess) weighted 70%, and
        # lower share price (more quantity per Rs.80,000 trade) weighted 30%. Excludes
        # anything with <1% average daily range (too sleepy) or <Rs.5 (penny-stock risk).
        "IDEA.NS":        "NSE_EQ|INE669E01016",  # ~Rs.14, 3.4% avg daily range
        "RPOWER.NS":      "NSE_EQ|INE614G01033",  # ~Rs.25, 4.2% avg daily range
        "UCOBANK.NS":     "NSE_EQ|INE691A01018",  # ~Rs.27, 2.9% avg daily range
        "MSUMI.NS":       "NSE_EQ|INE0FS801015",  # ~Rs.41, 2.8% avg daily range
        "TTML.NS":        "NSE_EQ|INE517B01013",  # ~Rs.42, 3.5% avg daily range
        "OLAELEC.NS":     "NSE_EQ|INE0LXG01040",  # ~Rs.42, 5.4% avg daily range
        "SUZLON.NS":      "NSE_EQ|INE040H01021",  # ~Rs.55, 2.8% avg daily range
        "ABFRL.NS":       "NSE_EQ|INE647O01011",  # ~Rs.62, 2.9% avg daily range
        "MMTC.NS":        "NSE_EQ|INE123F01029",  # ~Rs.66, 4.3% avg daily range
        "IFCI.NS":        "NSE_EQ|INE039A01010",  # ~Rs.75, 5.9% avg daily range
        "IDBI.NS":        "NSE_EQ|INE008A01015",  # ~Rs.84, 4.4% avg daily range
        "MAHABANK.NS":    "NSE_EQ|INE457A01014",  # ~Rs.87, 3.1% avg daily range
        "INOXWIND.NS":    "NSE_EQ|INE066P01011",  # ~Rs.87, 3.1% avg daily range
        "NBCC.NS":        "NSE_EQ|INE095N01031",  # ~Rs.100, 3.1% avg daily range
        "ZEEL.NS":        "NSE_EQ|INE256A01028",  # ~Rs.102, 5.1% avg daily range
        "DEVYANI.NS":     "NSE_EQ|INE872J01023",  # ~Rs.114, 3.1% avg daily range
        "JMFINANCIL.NS":  "NSE_EQ|INE780C01023",  # ~Rs.132, 2.9% avg daily range
        "URBANCO.NS":     "NSE_EQ|INE0CAZ01013",  # ~Rs.134, 3.1% avg daily range
        "PWL.NS":         "NSE_EQ|INE0LP301011",  # ~Rs.135, 4.5% avg daily range
        "CANHLIFE.NS":    "NSE_EQ|INE01TY01017",  # ~Rs.142, 2.9% avg daily range
        "PARADEEP.NS":    "NSE_EQ|INE088F01024",  # ~Rs.143, 3.0% avg daily range
        "MRPL.NS":        "NSE_EQ|INE103A01014",  # ~Rs.151, 3.4% avg daily range
        "PINELABS.NS":    "NSE_EQ|INE15B701018",  # ~Rs.151, 4.0% avg daily range
        "J&KBANK.NS":     "NSE_EQ|INE168A01041",  # ~Rs.164, 3.2% avg daily range
        "SAIL.NS":        "NSE_EQ|INE114A01011",  # ~Rs.169, 2.9% avg daily range
        "WELSPUNLIV.NS":  "NSE_EQ|INE192B01031",  # ~Rs.170, 3.7% avg daily range
        "SAMMAANCAP.NS":  "NSE_EQ|INE148I01020",  # ~Rs.177, 3.3% avg daily range
        "PPLPHARMA.NS":   "NSE_EQ|INE0DK501011",  # ~Rs.178, 3.6% avg daily range
        "SAPPHIRE.NS":    "NSE_EQ|INE806T01020",  # ~Rs.184, 3.6% avg daily range
        "NIACL.NS":       "NSE_EQ|INE470Y01017",  # ~Rs.186, 5.0% avg daily range
        "MEESHO.NS":      "NSE_EQ|INE0VDM01015",  # ~Rs.190, 4.5% avg daily range
        "GROWW.NS":       "NSE_EQ|INE0HOQ01053",  # ~Rs.200, 3.0% avg daily range
        "BANDHANBNK.NS":  "NSE_EQ|INE545U01014",  # ~Rs.207, 3.4% avg daily range
        "RITES.NS":       "NSE_EQ|INE320J01015",  # ~Rs.216, 3.0% avg daily range
        "FIRSTCRY.NS":    "NSE_EQ|INE02RE01045",  # ~Rs.216, 3.1% avg daily range
        "BELRISE.NS":     "NSE_EQ|INE894V01022",  # ~Rs.222, 3.6% avg daily range
        "CUB.NS":         "NSE_EQ|INE491A01021",  # ~Rs.225, 3.6% avg daily range
        "HFCL.NS":        "NSE_EQ|INE548A01028",  # ~Rs.225, 4.0% avg daily range
        "CGCL.NS":        "NSE_EQ|INE180C01042",  # ~Rs.242, 3.1% avg daily range
        "ENGINERSIN.NS":  "NSE_EQ|INE510A01028",  # ~Rs.244, 3.7% avg daily range
        "SWIGGY.NS":      "NSE_EQ|INE00H001014",  # ~Rs.248, 3.1% avg daily range
        "FSL.NS":         "NSE_EQ|INE684F01012",  # ~Rs.249, 3.4% avg daily range
        "BLS.NS":         "NSE_EQ|INE153T01027",  # ~Rs.250, 3.0% avg daily range
        "JINDALSAW.NS":   "NSE_EQ|INE324A01032",  # ~Rs.260, 3.6% avg daily range
        "REDINGTON.NS":   "NSE_EQ|INE891D01026",  # ~Rs.277, 3.9% avg daily range
        "SONATSOFTW.NS":  "NSE_EQ|INE269A01021",  # ~Rs.277, 4.4% avg daily range
        "AEGISVOPAK.NS":  "NSE_EQ|INE0INX01018",  # ~Rs.280, 4.8% avg daily range
        "BSOFT.NS":       "NSE_EQ|INE836A01035",  # ~Rs.281, 3.1% avg daily range
        "VEDL.NS":        "NSE_EQ|INE205A01025",  # ~Rs.282, 3.0% avg daily range
        "APTUS.NS":       "NSE_EQ|INE852O01025",  # ~Rs.286, 3.0% avg daily range
        "LATENTVIEW.NS":  "NSE_EQ|INE0I7C01011",  # ~Rs.286, 3.5% avg daily range
        "SCI.NS":         "NSE_EQ|INE109A01011",  # ~Rs.294, 3.4% avg daily range
        "KARURVYSYA.NS":  "NSE_EQ|INE036D01028",  # ~Rs.304, 3.1% avg daily range
        "GODIGIT.NS":     "NSE_EQ|INE03JT01014",  # ~Rs.312, 3.0% avg daily range
        "NUVOCO.NS":      "NSE_EQ|INE118D01016",  # ~Rs.318, 3.1% avg daily range
        "PCBL.NS":        "NSE_EQ|INE602A01031",  # ~Rs.322, 2.9% avg daily range
        "JSWINFRA.NS":    "NSE_EQ|INE880J01026",  # ~Rs.324, 3.3% avg daily range
        "M&MFIN.NS":      "NSE_EQ|INE774D01024",  # ~Rs.332, 3.3% avg daily range
        "SWANCORP.NS":    "NSE_EQ|INE665A01038",  # ~Rs.334, 3.0% avg daily range
        "TARIL.NS":       "NSE_EQ|INE763I01026",  # ~Rs.341, 4.3% avg daily range
        "ANGELONE.NS":    "NSE_EQ|INE732I01021",  # ~Rs.346, 3.1% avg daily range
        "MANAPPURAM.NS":  "NSE_EQ|INE522D01027",  # ~Rs.346, 3.1% avg daily range
        "TATACAP.NS":     "NSE_EQ|INE976I01016",  # ~Rs.350, 3.4% avg daily range
        "EMMVEE.NS":      "NSE_EQ|INE1C6T01020",  # ~Rs.351, 4.1% avg daily range
        "NATIONALUM.NS":  "NSE_EQ|INE139A01034",  # ~Rs.351, 3.0% avg daily range
        "JAINREC.NS":     "NSE_EQ|INE0YD401026",  # ~Rs.352, 5.4% avg daily range
        "IGIL.NS":        "NSE_EQ|INE0Q9301021",  # ~Rs.356, 3.1% avg daily range
        "KALYANKJIL.NS":  "NSE_EQ|INE303R01014",  # ~Rs.381, 3.3% avg daily range
        "ACMESOLAR.NS":   "NSE_EQ|INE622W01025",  # ~Rs.384, 4.1% avg daily range
        "RHIM.NS":        "NSE_EQ|INE743M01012",  # ~Rs.385, 4.3% avg daily range
        "BHEL.NS":        "NSE_EQ|INE257A01026",  # ~Rs.388, 3.2% avg daily range
        "JKTYRE.NS":      "NSE_EQ|INE573A01042",  # ~Rs.409, 3.0% avg daily range
        "TMCV.NS":        "NSE_EQ|INE1TAE01010",  # ~Rs.430, 3.2% avg daily range (Tata Motors Commercial Vehicles)
        "COHANCE.NS":     "NSE_EQ|INE03QK01018",  # ~Rs.453, 3.8% avg daily range
        "HONASA.NS":      "NSE_EQ|INE0J5401028",  # ~Rs.469, 3.6% avg daily range
        "POONAWALLA.NS":  "NSE_EQ|INE511C01022",  # ~Rs.476, 3.0% avg daily range
        "ZENSARTECH.NS":  "NSE_EQ|INE520A01027",  # ~Rs.478, 4.0% avg daily range
        "NEWGEN.NS":      "NSE_EQ|INE619B01017",  # ~Rs.480, 3.1% avg daily range
        "SUMICHEM.NS":    "NSE_EQ|INE258G01013",  # ~Rs.484, 3.6% avg daily range
        "SAREGAMA.NS":    "NSE_EQ|INE979A01025",  # ~Rs.488, 3.6% avg daily range
        "AARTIIND.NS":    "NSE_EQ|INE769A01020",  # ~Rs.488, 3.4% avg daily range
        "THELEELA.NS":    "NSE_EQ|INE0AQ201015",  # ~Rs.502, 3.8% avg daily range
        "BERGEPAINT.NS":  "NSE_EQ|INE463A01038",  # ~Rs.522, 3.2% avg daily range
        "AIIL.NS":        "NSE_EQ|INE206F01022",  # ~Rs.522, 4.0% avg daily range
        "IIFL.NS":        "NSE_EQ|INE530B01024",  # ~Rs.528, 3.4% avg daily range
        "ELECON.NS":      "NSE_EQ|INE205B01031",  # ~Rs.531, 3.6% avg daily range
        "HEXT.NS":        "NSE_EQ|INE093A01041",  # ~Rs.536, 3.7% avg daily range
        "BRIGADE.NS":     "NSE_EQ|INE791I01019",  # ~Rs.537, 3.5% avg daily range
        "FIVESTAR.NS":    "NSE_EQ|INE128S01021",  # ~Rs.554, 4.1% avg daily range
        "AADHARHFC.NS":   "NSE_EQ|INE883F01010",  # ~Rs.556, 3.2% avg daily range
        "KPITTECH.NS":    "NSE_EQ|INE04I401011",  # ~Rs.558, 3.4% avg daily range
        "BLUEJET.NS":     "NSE_EQ|INE0KBH01020",  # ~Rs.562, 4.2% avg daily range
        "TEJASNET.NS":    "NSE_EQ|INE010J01012",  # ~Rs.570, 4.5% avg daily range
        "PGEL.NS":        "NSE_EQ|INE457L01029",  # ~Rs.570, 3.1% avg daily range
        "ZYDUSWELL.NS":   "NSE_EQ|INE768C01028",  # ~Rs.583, 4.1% avg daily range
        "TENNIND.NS":     "NSE_EQ|INE19RI01016",  # ~Rs.593, 3.5% avg daily range
        "GRAPHITE.NS":    "NSE_EQ|INE371A01025",  # ~Rs.620, 3.2% avg daily range
        "TRITURBINE.NS":  "NSE_EQ|INE152M01016",  # ~Rs.649, 4.1% avg daily range
        "VTL.NS":         "NSE_EQ|INE825A01020",  # ~Rs.662, 3.4% avg daily range
        "GALLANTT.NS":    "NSE_EQ|INE297H01019",  # ~Rs.669, 3.5% avg daily range
        "HSCL.NS":        "NSE_EQ|INE019C01026",  # ~Rs.684, 4.5% avg daily range
        "BATAINDIA.NS":   "NSE_EQ|INE176A01028",  # ~Rs.689, 3.3% avg daily range
        "JBMA.NS":        "NSE_EQ|INE927D01051",  # ~Rs.692, 3.1% avg daily range
        "ABDL.NS":        "NSE_EQ|INE552Z01027",  # ~Rs.703, 3.7% avg daily range
        "TATATECH.NS":    "NSE_EQ|INE142M01025",  # ~Rs.712, 3.2% avg daily range
        "ATGL.NS":        "NSE_EQ|INE399L01023",  # ~Rs.724, 3.3% avg daily range
        "SPLPETRO.NS":    "NSE_EQ|INE663A01033",  # ~Rs.728, 3.2% avg daily range
        "INTELLECT.NS":   "NSE_EQ|INE306R01017",  # ~Rs.769, 3.3% avg daily range
        "JYOTICNC.NS":    "NSE_EQ|INE980O01024",  # ~Rs.771, 3.8% avg daily range
        "SIGNATURE.NS":   "NSE_EQ|INE903U01023",  # ~Rs.789, 3.3% avg daily range
        "ASTERDM.NS":     "NSE_EQ|INE914M01019",  # ~Rs.792, 3.5% avg daily range
        "GRANULES.NS":    "NSE_EQ|INE101D01020",  # ~Rs.854, 3.1% avg daily range
        "TITAGARH.NS":    "NSE_EQ|INE615H01020",  # ~Rs.868, 3.2% avg daily range
        "FACT.NS":        "NSE_EQ|INE188A01015",  # ~Rs.878, 3.7% avg daily range
        "MAPMYINDIA.NS":  "NSE_EQ|INE0BV301023",  # ~Rs.947, 4.3% avg daily range
        "ACE.NS":         "NSE_EQ|INE731H01025",  # ~Rs.1006, 3.2% avg daily range
        "FINCABLES.NS":   "NSE_EQ|INE235A01022",  # ~Rs.1087, 4.1% avg daily range
        "CHENNPETRO.NS":  "NSE_EQ|INE178A01016",  # ~Rs.1120, 3.6% avg daily range
        "ATHERENERG.NS":  "NSE_EQ|INE0LEZ01016",  # ~Rs.1125, 4.2% avg daily range
        "CARBORUNIV.NS":  "NSE_EQ|INE120A01034",  # ~Rs.1144, 4.5% avg daily range
        "ABSLAMC.NS":     "NSE_EQ|INE404A01024",  # ~Rs.1161, 4.2% avg daily range
        "KPRMILL.NS":     "NSE_EQ|INE930H01031",  # ~Rs.1179, 4.8% avg daily range
        "HOMEFIRST.NS":   "NSE_EQ|INE481N01025",  # ~Rs.1191, 3.6% avg daily range
        "CCL.NS":         "NSE_EQ|INE421D01022",  # ~Rs.1200, 3.8% avg daily range
        "GODREJIND.NS":   "NSE_EQ|INE233A01035",  # ~Rs.1209, 3.3% avg daily range
        "GABRIEL.NS":     "NSE_EQ|INE524A01029",  # ~Rs.1275, 4.2% avg daily range
        "CONCORDBIO.NS":  "NSE_EQ|INE338H01029",  # ~Rs.1284, 3.3% avg daily range
        "TRAVELFOOD.NS":  "NSE_EQ|INE103V01028",  # ~Rs.1330, 3.6% avg daily range
        "VIJAYA.NS":      "NSE_EQ|INE043W01024",  # ~Rs.1347, 4.0% avg daily range
        "KPIL.NS":        "NSE_EQ|INE220B01022",  # ~Rs.1380, 3.3% avg daily range
        "SYRMA.NS":       "NSE_EQ|INE0DYJ01015",  # ~Rs.1391, 3.9% avg daily range
        "AEGISLOG.NS":    "NSE_EQ|INE208C01025",  # ~Rs.1409, 6.1% avg daily range
        "GESHIP.NS":      "NSE_EQ|INE017A01032",  # ~Rs.1410, 3.8% avg daily range
        "ABREL.NS":       "NSE_EQ|INE055A01016",  # ~Rs.1418, 3.4% avg daily range
        "OLECTRA.NS":     "NSE_EQ|INE260D01016",  # ~Rs.1479, 4.0% avg daily range
        "ECLERX.NS":      "NSE_EQ|INE738I01010",  # ~Rs.1498, 3.8% avg daily range
        "SCHNEIDER.NS":   "NSE_EQ|INE839M01018",  # ~Rs.1501, 5.5% avg daily range
        "TBOTEK.NS":      "NSE_EQ|INE673O01025",  # ~Rs.1519, 3.8% avg daily range
        "CEMPRO.NS":      "NSE_EQ|INE686A01026",  # ~Rs.1521, 4.6% avg daily range
        "WELCORP.NS":     "NSE_EQ|INE191B01025",  # ~Rs.1544, 3.6% avg daily range
        "DEEPAKFERT.NS":  "NSE_EQ|INE501A01019",  # ~Rs.1605, 3.4% avg daily range
        "ONESOURCE.NS":   "NSE_EQ|INE013P01021",  # ~Rs.1663, 3.7% avg daily range
        "POLYMED.NS":     "NSE_EQ|INE205C01021",  # ~Rs.1688, 4.0% avg daily range
        "ZENTEC.NS":      "NSE_EQ|INE251B01027",  # ~Rs.1853, 4.2% avg daily range
        "TATACOMM.NS":    "NSE_EQ|INE151A01013",  # ~Rs.1892, 3.6% avg daily range
        "WOCKPHARMA.NS":  "NSE_EQ|INE049B01025",  # ~Rs.1916, 3.9% avg daily range
        "KIRLOSENG.NS":   "NSE_EQ|INE146L01010",  # ~Rs.2266, 4.8% avg daily range
        "RRKABEL.NS":     "NSE_EQ|INE777K01022",  # ~Rs.2295, 3.8% avg daily range
        "ZFCVINDIA.NS":   "NSE_EQ|INE342J01019",  # ~Rs.2400, 4.0% avg daily range
        "GLAND.NS":       "NSE_EQ|INE068V01023",  # ~Rs.2463, 3.4% avg daily range
        "CAPLIPOINT.NS":  "NSE_EQ|INE475E01026",  # ~Rs.2624, 4.2% avg daily range
        "CARTRADE.NS":    "NSE_EQ|INE290S01011",  # ~Rs.2791, 5.5% avg daily range
        "ENRIN.NS":       "NSE_EQ|INE1NPP01017",  # ~Rs.3420, 4.0% avg daily range
        "ACUTAAS.NS":     "NSE_EQ|INE00FF01025",  # ~Rs.3515, 4.2% avg daily range
        "CPPLUS.NS":      "NSE_EQ|INE819V01029",  # ~Rs.3611, 4.0% avg daily range
        "NETWEB.NS":      "NSE_EQ|INE0NT901020",  # ~Rs.4412, 4.7% avg daily range
        "GVT&D.NS":       "NSE_EQ|INE200A01026",  # ~Rs.4554, 4.6% avg daily range
        "DATAPATTNS.NS":  "NSE_EQ|INE0IX101010",  # ~Rs.4616, 4.7% avg daily range
        "THERMAX.NS":     "NSE_EQ|INE152A01029",  # ~Rs.4736, 3.9% avg daily range
        "APARINDS.NS":    "NSE_EQ|INE372A01015",  # ~Rs.14323, 4.6% avg daily range
    }

    @classmethod
    def validate(cls):
        """Validates that OAuth credentials are set when running in LIVE or PAPER mode."""
        if cls.TRADING_MODE in ["LIVE", "PAPER"]:
            missing = [k for k in ["CLIENT_ID", "CLIENT_SECRET"] if not getattr(cls, k)]
            if missing:
                raise ValueError(f"Missing required credentials in .env: {', '.join(missing)}")
        return True

    @classmethod
    def is_market_hours(cls) -> bool:
        """
        True if current IST time is within NSE trading hours (09:15-15:20) on a weekday.
        VIRTUAL mode always returns True so the demo simulator can run around the clock.
        """
        if cls.TRADING_MODE == "VIRTUAL":
            return True

        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)

        if now.weekday() >= 5:
            return False

        market_start = time(9, 15)
        market_end = time(15, 20)
        return market_start <= now.time() <= market_end
