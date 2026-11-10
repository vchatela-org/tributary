"""What kind of use an app is, for App Usage's `category` tag.

The phone app's own categories don't say how a phone is used: it files a
browser and a mail client under SOCIAL, most apps under none at all, and
cannot tell a video watched from a feed scrolled. So Tributary files every app
under one word of its own vocabulary, CATEGORIES, at import:

  1. TRIBUTARY_APP_USAGE_CATEGORIES, {"<package>": "<category>"}: yours;
  2. the category you chose for the app in App Usage, when you chose one and it
     maps to one of these words (the app's own words, by APP_USAGE_WORDS, or a
     category you created under one of these names);
  3. KNOWN, below: well-known apps, by package;
  4. the category App Usage gives the app, by APP_USAGE_WORDS;
  5. `system` for the phone maker's and Android's own packages, SYSTEM_PREFIXES;
  6. `other`.

LEAN_BACK are the categories where the screen is on without the phone being in
hand: the home screen between two apps, a video, music, a map while driving.
The dashboards count the rest as hands-on time.
"""
from typing import Dict, Optional, Tuple

CATEGORIES: Tuple[str, ...] = (
    "launcher", "watching", "listening", "social", "communication", "browsing", "productivity", "navigation",
    "smart_home", "health", "shopping", "finance", "games", "news", "photos", "system", "other",
)
LEAN_BACK: Tuple[str, ...] = ("launcher", "watching", "listening", "navigation")
OTHER = "other"

# App Usage's built-in categories (categories.csv, upper case) to ours. OTHERS
# is no category at all, and ACCESSIBILITY a phone setting.
APP_USAGE_WORDS: Dict[str, Optional[str]] = {
    "game": "games", "audio": "listening", "video": "watching", "image": "photos", "social": "social",
    "news": "news", "maps": "navigation", "productivity": "productivity", "accessibility": "system",
    "others": None,
}

SYSTEM_PREFIXES: Tuple[str, ...] = (
    "android", "com.android.", "com.google.android.gms", "com.google.android.ext.", "com.miui.", "com.xiaomi.",
    "com.samsung.android.", "com.sec.android.", "com.oneplus.", "com.huawei.", "com.oplus.", "com.coloros.",
    "com.qualcomm.", "com.mediatek.",
)

_KNOWN: Dict[str, Tuple[str, ...]] = {
    "launcher": (
        "com.teslacoilsw.launcher", "com.miui.home", "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher", "com.microsoft.launcher", "app.lawnchair", "ch.deletescape.lawnchair.plah",
        "bitpit.launcher", "com.oneplus.launcher", "com.huawei.android.launcher", "com.nothing.launcher",
        "com.android.launcher3",
    ),
    "watching": (
        "com.google.android.youtube", "com.google.android.apps.youtube.kids", "app.revanced.android.youtube",
        "com.netflix.mediaclient", "com.plexapp.android", "com.disney.disneyplus",
        "com.amazon.avod.thirdpartyclient", "tv.twitch.android.app", "com.canal.android.canal",
        "com.crunchyroll.crunchyroid", "com.dailymotion.dailymotion", "org.jellyfin.mobile",
        "org.videolan.vlc", "com.mxtech.videoplayer.ad", "com.google.android.videos", "tv.arte.plus7",
        "com.wbd.stream",
    ),
    "listening": (
        "com.spotify.music", "deezer.android.app", "com.google.android.apps.youtube.music",
        "com.audible.application", "com.audiobookshelf.app", "au.com.shiftyjelly.pocketcasts",
        "com.soundcloud.android", "com.apple.android.music", "com.amazon.mp3",
    ),
    "social": (
        "com.twitter.android", "com.facebook.katana", "com.instagram.android", "com.instagram.barcelona",
        "com.reddit.frontpage", "com.linkedin.android", "com.zhiliaoapp.musically", "com.ss.android.ugc.trill",
        "com.snapchat.android", "com.pinterest", "com.tumblr", "org.joinmastodon.android", "xyz.blueskyweb.app",
    ),
    "communication": (
        "com.whatsapp", "org.thoughtcrime.securesms", "org.telegram.messenger", "com.facebook.orca",
        "com.discord", "com.google.android.apps.messaging", "com.google.android.gm",
        "com.microsoft.office.outlook", "ch.protonmail.android", "com.google.android.dialer",
        "com.samsung.android.messaging", "com.samsung.android.dialer", "com.microsoft.teams", "com.Slack",
        "us.zoom.videomeetings", "com.google.android.apps.tachyon", "com.skype.raider", "com.viber.voip",
    ),
    "browsing": (
        "com.android.chrome", "com.brave.browser", "org.mozilla.firefox", "com.microsoft.emmx",
        "com.opera.browser", "com.duckduckgo.mobile.android", "com.vivaldi.browser",
        "com.sec.android.app.sbrowser", "com.google.android.googlequicksearchbox",
    ),
    "productivity": (
        "com.github.android", "com.termux", "com.todoist", "notion.id",
        "com.google.android.calendar", "com.google.android.keep", "com.google.android.apps.docs",
        "com.google.android.apps.docs.editors.docs", "com.google.android.apps.docs.editors.sheets",
        "com.microsoft.office.officehubrow", "com.microsoft.office.word", "com.microsoft.office.excel",
        "com.microsoft.office.powerpoint", "com.adobe.reader", "com.dropbox.android", "com.anthropic.claude",
        "com.openai.chatgpt", "com.google.android.apps.bard",
    ),
    "navigation": (
        "com.google.android.apps.maps", "com.waze", "com.citymapper.app.release", "com.ubercab",
    ),
    "smart_home": (
        "io.homeassistant.companion.android",
        "com.philips.lighting.hue2", "com.google.android.apps.chromecast.app", "com.amazon.dee.app",
        "com.xiaomi.smarthome",
    ),
    "health": (
        "com.whoop.android", "com.fitbit.FitbitMobile", "eu.smartpatient.mytherapy",
        "com.medisafe.android.client", "com.garmin.android.apps.connectmobile", "com.strava",
        "com.myfitnesspal.android", "com.urbandroid.sleep", "com.google.android.apps.fitness",
        "com.sec.android.app.shealth",
    ),
    "shopping": (
        "com.amazon.mShop.android.shopping", "fr.leboncoin", "fr.vinted",
        "com.ubercab.eats", "com.alibaba.aliexpresshd", "com.ebay.mobile", "com.einnovation.temu",
    ),
    "finance": (
        "com.paypal.android.p2pmobile", "com.revolut.revolut", "de.number26.android",
    ),
    "news": (
        "com.google.android.apps.magazines", "flipboard.app",
    ),
    "photos": (
        "com.android.camera", "com.google.android.GoogleCamera", "com.sec.android.app.camera",
        "com.miui.gallery", "com.google.android.apps.photos", "com.sec.android.gallery3d",
    ),
    "system": (
        "com.android.settings", "com.android.deskclock", "com.google.android.deskclock", "com.android.vending",
        "com.google.android.apps.nbu.files", "com.x8bit.bitwarden", "com.authy.authy",
        "com.google.android.apps.authenticator2",
        "com.a0soft.gphone.uninstaller",
    ),
}
KNOWN: Dict[str, str] = {package: category for category, packages in _KNOWN.items() for package in packages}


def categorize(package: str, chosen: Optional[str], own: Optional[str], mine: Dict[str, str]) -> str:
    """The app's category. `chosen`: the name of the category the user gave it
    in App Usage (None when they gave none); `own`: App Usage's; `mine`:
    TRIBUTARY_APP_USAGE_CATEGORIES, already checked."""
    if package in mine:
        return mine[package]
    if chosen:
        word = chosen.strip().lower()
        word = word if word in CATEGORIES else APP_USAGE_WORDS.get(word)
        if word:
            return word
    if package in KNOWN:
        return KNOWN[package]
    word = APP_USAGE_WORDS.get((own or "").strip().lower())
    if word:
        return word
    if package.startswith(SYSTEM_PREFIXES):
        return "system"
    return OTHER
