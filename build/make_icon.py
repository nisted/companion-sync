"""Build step: draw the app icon (a green 'C' ring)."""
from PIL import Image, ImageDraw
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(28, 32, 40, 255))
d.ellipse((200, 200, S - 200, S - 200), fill=(46, 160, 67, 255))
d.ellipse((360, 360, S - 360, S - 360), fill=(28, 32, 40, 255))
d.rectangle((600, 440, S - 180, 584), fill=(28, 32, 40, 255))
img.save("icon.png")
