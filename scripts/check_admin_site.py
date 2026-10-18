import re
import urllib.request

html = urllib.request.urlopen("https://admin-vert-gamma-35.vercel.app/").read().decode()
match = re.search(r'src="([^"]+\.js)"', html)
src = match.group(1) if match else ""
js = urllib.request.urlopen("https://admin-vert-gamma-35.vercel.app" + src).read().decode()
print(src)
print("connected" if "trycloudflare.com" in js else "missing")
