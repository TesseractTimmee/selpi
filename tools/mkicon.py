# selpi icon: battery outline with a lightning bolt, transparent background (HA brand-style square).
import sys
from PIL import Image, ImageDraw
def icon(size, body, bolt, out):
    S = 4 * size                                   # draw big, downsample for smooth edges
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    u = S / 256.0
    w = 14 * u                                     # outline width
    d.rounded_rectangle([40*u, 64*u, 204*u, 200*u], radius=22*u, outline=body, width=int(w))   # battery body
    d.rounded_rectangle([204*u, 106*u, 226*u, 158*u], radius=6*u, fill=body)                   # terminal
    pts = [(136,86),(92,138),(120,138),(106,180),(156,122),(127,122),(144,86)]               # bolt, inside the body
    d.polygon([(x*u, y*u) for x, y in pts], fill=bolt)
    im.resize((size, size), Image.LANCZOS).save(out)
B = sys.argv[1]
light_body, dark_body, bolt = (33, 61, 92, 255), (220, 228, 236, 255), (245, 166, 35, 255)
icon(256, light_body, bolt, B + '/icon.png');      icon(512, light_body, bolt, B + '/icon@2x.png')
icon(256, dark_body, bolt, B + '/dark_icon.png');  icon(512, dark_body, bolt, B + '/dark_icon@2x.png')
