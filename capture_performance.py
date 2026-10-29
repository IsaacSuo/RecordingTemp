"""Bounded preview processing and lightweight aggregate capture diagnostics."""
import threading
import time
from PIL import Image


class StageTimings:
    def __init__(self):
        self.values = {}

    def call(self, name, function, *args):
        start = time.perf_counter_ns()
        try:
            return function(*args)
        finally:
            elapsed = time.perf_counter_ns()-start
            count, total, maximum = self.values.get(name, (0, 0, 0))
            self.values[name] = (count+1, total+elapsed, max(maximum, elapsed))

    def summary(self):
        return {name: dict(calls=count, total_ms=total/1e6, mean_ms=total/count/1e6, max_ms=maximum/1e6)
                for name, (count, total, maximum) in self.values.items()}


class PreviewProcessor:
    """Latest-only RGB input/output mailboxes: preview never blocks acquisition."""
    def __init__(self, name):
        self.condition = threading.Condition()
        self.pending = self.result = None
        self.closed = False
        self.error = None
        self.thread = threading.Thread(target=self.run, name='preview-'+name, daemon=True)
        self.thread.start()

    def submit(self, pixels, shape, full, view=None):
        with self.condition:
            if not self.closed:
                self.pending = (pixels, shape, full, view)
                self.condition.notify()

    def take(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    @staticmethod
    def convert(pixels, shape, full, view=None):
        image = Image.frombytes('RGB', shape, pixels)
        if view:
            if bool(view.get('rotate180')) ^ bool(view.get('mirror_horizontal')):
                image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if bool(view.get('rotate180')) ^ bool(view.get('mirror_vertical')):
                image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if not full:
            image.thumbnail((540, 300), Image.Resampling.BILINEAR)
        return image

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                item, self.pending = self.pending, None
            try:
                result = self.convert(*item)
                with self.condition:
                    self.result = result
            except Exception as exc:
                self.error = exc
                return

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = self.result = None
            self.condition.notify()
        self.thread.join(timeout=5)
        with self.condition:
            self.result = None
