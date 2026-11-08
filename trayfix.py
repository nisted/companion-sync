"""Linux tray icons that the Pi's taskbar can draw.

pystray's AppIndicator backend writes each icon picture to a new temporary
file with no extension (tempfile.mktemp), deletes the previous one, and hands
the taskbar that path as the icon's *name*. On BD-1 (Raspberry Pi OS, labwc +
wf-panel-pi) the taskbar gave the icon a slot but drew nothing (7 Nov).

Here each picture is saved once as a named .png and the taskbar gets the
folder (IconThemePath) plus an icon name without a path, the way the
StatusNotifierItem spec expects. Each distinct picture gets its own name, so
a status change is a new name and the taskbar redraws.

The folder must be one the desktop's default icon lookup also searches
(~/.local/share/icons): wf-panel-pi checks the name against IconThemePath but
then loads it with the default icon theme only (src/widgets/tray/item.cpp
update_icon -> panel/plugin.c load_taskbar_pixbuf), so a picture found only
in IconThemePath is never drawn (v0.4.3 on BD-1, 7 Nov).

Only the AppIndicator backend is changed; on Windows and the Mac nothing here
runs. If anything goes wrong, pystray's own way is left in place.
"""
import hashlib
import io
import logging
import os

log = logging.getLogger("rebeldeck.tray")


def install(pystray_module, app_name, folder):
    """Patch pystray's AppIndicator Icon class, if that's the backend in use.
    Call before creating the pystray.Icon. Returns True if patched."""
    cls = getattr(pystray_module, "Icon", None)
    if cls is None or not cls.__module__.endswith("_appindicator"):
        return False
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        log.warning("tray icons: can't make %s (%s); using pystray's own", folder, e)
        return False

    def _update_fs_icon(self):
        name, data = _named(app_name, self.icon)
        path = os.path.join(folder, name + ".png")
        if not os.path.exists(path):
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        try:
            self._appindicator.set_icon_theme_path(folder)
        except Exception as e:                      # older AppIndicator bindings
            log.debug("tray icons: set_icon_theme_path: %s", e)
        self._icon_path = name                      # an icon name, not a file path

    def _remove_fs_icon(self):
        self._icon_path = None                      # the files stay: a handful of small PNGs

    cls._update_fs_icon = _update_fs_icon
    cls._remove_fs_icon = _remove_fs_icon
    log.info("tray icons: named PNGs in %s", folder)
    return True


def _slug(app_name):
    return "".join(c for c in app_name.lower() if c.isalnum()) or "app"


def _named(app_name, image):
    """(icon name, PNG bytes) for a picture: the name follows the picture."""
    buf = io.BytesIO()
    image.save(buf, "PNG")
    data = buf.getvalue()
    return f"{_slug(app_name)}-{hashlib.sha1(data).hexdigest()[:10]}", data


def tidy(folder, app_name, images):
    """Remove pictures (from older versions) that none of `images` would use."""
    keep = {_named(app_name, im)[0] for im in images}
    slug = _slug(app_name)
    try:
        for f in os.listdir(folder):
            if f.startswith(slug + "-") and f.endswith(".png") and f[:-4] not in keep:
                os.remove(os.path.join(folder, f))
    except OSError:
        pass
