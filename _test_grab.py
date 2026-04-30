import threading
from PIL import ImageGrab

result = []

def grab():
    try:
        img = ImageGrab.grab(bbox=(0, 0, 300, 300), all_screens=True)
        result.append(('ok', img.size, img.mode))
    except Exception as e:
        import traceback
        result.append(('err', traceback.format_exc()))

t = threading.Thread(target=grab)
t.start()
t.join()
print(result)
