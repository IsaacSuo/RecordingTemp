"""MindVision example: --list, --preview, or a timed raw capture benchmark.

Install the official Windows SDK first. Preview requires numpy and opencv-python.
Run: python camera_demo.py --preview --seconds 60
"""
import argparse
import ctypes
import platform
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--exposure-us', type=float, default=None,
                        help='Optional manual exposure in microseconds')
    parser.add_argument('--speed-index', type=int, default=None,
                        help='SDK speed mode index, NOT a target FPS')
    args = parser.parse_args()
    if args.seconds <= 0 or (args.exposure_us is not None and args.exposure_us <= 0):
        parser.error('Duration and exposure must be positive')
    try:
        import mvsdk as sdk
    except (ImportError, OSError, AttributeError) as exc:
        raise SystemExit(f'Cannot load SDK: {exc}\nInstall the official SDK/driver matching Python bitness.')

    def check(code):
        if code != sdk.CAMERA_STATUS_SUCCESS:
            raise sdk.CameraException(code)

    check(sdk.CameraSdkInit(1))
    devices = sdk.CameraEnumerateDevice()
    check(sdk.GetLastError())
    for index, device in enumerate(devices):
        print(index, device.GetFriendlyName(), device.GetProductName(), device.GetPortType())
    if not devices:
        raise SystemExit('No camera found. Check the driver, cable and other camera applications.')
    if args.list:
        return
    if not 0 <= args.device < len(devices):
        parser.error('Invalid device index')
    if args.preview:
        import cv2
        import numpy as np

    camera, output = None, None
    try:
        camera = sdk.CameraInit(devices[args.device], -1, -1)
        cap = sdk.CameraGetCapability(camera)
        check(sdk.GetLastError())
        print('Maximum size:', cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax)
        speeds = [cap.pFrameSpeedDesc[i] for i in range(cap.iFrameSpeedDesc)]
        print('Speed modes:', [(s.iIndex, s.GetDescription()) for s in speeds])
        check(sdk.CameraSetTriggerMode(camera, 0))  # Continuous acquisition
        if args.exposure_us is not None:
            check(sdk.CameraSetAeState(camera, 0))
            check(sdk.CameraSetExposureTime(camera, args.exposure_us))
        if args.speed_index is not None:
            if args.speed_index not in [s.iIndex for s in speeds]:
                raise ValueError('Choose a speed mode from the printed list')
            check(sdk.CameraSetFrameSpeed(camera, args.speed_index))
        channels = 1 if cap.sIspCapacity.bMonoSensor else 3
        if args.preview:
            fmt = sdk.CAMERA_MEDIA_TYPE_MONO8 if channels == 1 else sdk.CAMERA_MEDIA_TYPE_BGR8
            check(sdk.CameraSetIspOutFormat(camera, fmt))
            capacity = cap.sResolutionRange.iWidthMax * cap.sResolutionRange.iHeightMax * channels
            output = sdk.CameraAlignMalloc(capacity, 16)
            if not output:
                raise MemoryError('Cannot allocate preview buffer')
            cv2.namedWindow('MindVision', cv2.WINDOW_NORMAL)
        check(sdk.CameraPlay(camera))
        start = last_report = time.perf_counter()
        last_display = 0.0
        count = interval_count = timeouts = 0
        print('Capturing; Ctrl+C or preview Q/Esc to stop. Preview is limited to about 30 FPS.')
        while time.perf_counter() - start < args.seconds:
            try:
                raw, head = sdk.CameraGetImageBuffer(camera, 200)
            except sdk.CameraException as exc:
                if exc.error_code != sdk.CAMERA_STATUS_TIME_OUT:
                    raise
                timeouts += 1
                if args.preview and (cv2.waitKey(1) & 0xff) in (27, ord('q')):
                    break
                continue
            stop = False
            try:
                count += 1
                interval_count += 1
                now = time.perf_counter()
                if args.preview and now - last_display >= 1 / 30:
                    required = head.iWidth * head.iHeight * channels
                    if required <= 0 or required > capacity:
                        raise RuntimeError('Unexpected frame size')
                    check(sdk.CameraImageProcess(camera, raw, output, head))
                    if platform.system() == 'Windows':
                        check(sdk.CameraFlipFrameBuffer(output, head, 1))
                    frame = np.ctypeslib.as_array((ctypes.c_ubyte * required).from_address(output))
                    shape = (head.iHeight, head.iWidth) if channels == 1 else (head.iHeight, head.iWidth, 3)
                    cv2.imshow('MindVision', frame.reshape(shape))
                    stop = (cv2.waitKey(1) & 0xff) in (27, ord('q'))
                    last_display = now
            finally:
                check(sdk.CameraReleaseImageBuffer(camera, raw))
            if now - last_report >= 1:
                print(f'Received: {interval_count / (now - last_report):.1f} FPS; total={count}; timeouts={timeouts}')
                interval_count = 0
                last_report = now
            if stop:
                break
        elapsed = time.perf_counter() - start
        print(f'Received {count} frames / {elapsed:.2f}s = {count / elapsed:.1f} FPS; timeouts={timeouts}')
        stats = sdk.CameraGetFrameStatistic(camera)
        check(sdk.GetLastError())
        print(f'SDK counters: total={stats.iTotal}, valid={stats.iCapture}, lost={stats.iLost}')
    finally:
        if camera is not None:
            sdk.CameraUnInit(camera)
        if output:
            sdk.CameraAlignFree(output)
        if args.preview:
            cv2.destroyAllWindows()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
