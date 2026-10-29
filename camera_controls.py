"""Capability-based advanced controls; all SDK operations run on the camera worker."""
from PIL import Image


def orient_rgb(pixels, shape, rotate180=True, mirror_horizontal=False, mirror_vertical=False):
    horizontal = bool(rotate180) ^ bool(mirror_horizontal)
    vertical = bool(rotate180) ^ bool(mirror_vertical)
    if not horizontal and not vertical:
        return pixels
    if vertical and not horizontal:
        # The default 180-degree rotation + horizontal mirror is a row reversal.
        # Avoid RGB -> Pillow's padded RGB -> transpose -> RGB copies per frame.
        stride = shape[0] * 3
        if len(pixels) != stride * shape[1]:
            raise ValueError('RGB buffer size does not match image dimensions')
        view = memoryview(pixels)
        return b''.join(view[offset:offset+stride] for offset in range(len(pixels)-stride, -1, -stride))
    transform = (Image.Transpose.ROTATE_180 if horizontal and vertical else
                 Image.Transpose.FLIP_LEFT_RIGHT if horizontal else Image.Transpose.FLIP_TOP_BOTTOM)
    return Image.frombytes('RGB', shape, pixels).transpose(transform).tobytes()


def orientation(camera):
    return dict(rotate180=camera.settings().rotate180,
                mirror_horizontal=getattr(camera, 'mirror_horizontal', False),
                mirror_vertical=getattr(camera, 'mirror_vertical', False),
                stage='application after SDK ISP; applies to preview and MP4, not RAW')


def control(label, group, value=None, kind='int', minimum=None, maximum=None, choices=None, error=None):
    return dict(label=label, group=group, value=value, kind=kind, min=minimum, max=maximum,
                choices=choices, supported=error is None, error=error)


def sdk_controls(camera):
    s, h, cap, get = camera.s, camera.h, camera.cap, camera.sys.get
    current = camera.settings()
    fields = dict(
        exposure_ms=control('曝光时间（ms）', '曝光与方向', current.exposure_ms, 'float',
                            camera.exposure_range[0]/1000, camera.exposure_range[1]/1000),
        gain=control('模拟增益（倍）', '曝光与方向', current.gain, 'float', camera.gain_min, camera.gain_max),
        rotate180=control('画面旋转', '曝光与方向', current.rotate180, 'choice', choices=[(False, '0°'), (True, '180°')]),
        mirror_horizontal=control('水平镜像', '曝光与方向', camera.mirror_horizontal, 'bool'),
        mirror_vertical=control('垂直镜像', '曝光与方向', camera.mirror_vertical, 'bool'))

    def add(key, label, group, getter, kind='int', bounds=None, choices=None, args=(), applicable=True):
        try:
            if not applicable:
                raise ValueError('此相机不适用此功能')
            value = get(getattr(s, getter), h, *args)
            if kind == 'bool':
                value = bool(value)
            low, high = bounds if bounds else (None, None)
            fields[key] = control(label, group, value, kind, low, high, choices)
        except Exception as exc:
            fields[key] = control(label, group, kind=kind, choices=choices, error=str(exc))

    add('lut_mode', 'LUT 曲线模式', '色彩与亮度', 'CameraGetLutMode', 'choice',
        choices=[(0, '动态（Gamma / 对比度）'), (1, '相机预设曲线'), (2, '已加载的自定义曲线')])
    lut_choices = [(cap.pPresetLutDesc[i].iIndex, cap.pPresetLutDesc[i].GetDescription()) for i in range(cap.iPresetLut)]
    add('lut_preset', '预设 LUT', '色彩与亮度', 'CameraGetLutPresetSel', 'choice', choices=lut_choices,
        applicable=bool(lut_choices))
    for key, label, getter, range_name in (
            ('gamma', 'Gamma（SDK 整数）', 'CameraGetGamma', 'sGammaRange'),
            ('contrast', '对比度', 'CameraGetContrast', 'sContrastRange'),
            ('saturation', '饱和度', 'CameraGetSaturation', 'sSaturationRange'),
            ('sharpness', '锐化', 'CameraGetSharpness', 'sSharpnessRange')):
        limits = getattr(cap, range_name)
        add(key, label, '处理与速度' if key == 'sharpness' else '色彩与亮度', getter,
            bounds=(limits.iMin, limits.iMax), applicable=key != 'saturation' or not cap.sIspCapacity.bMonoSensor)
    add('black_level', '黑电平', '色彩与亮度', 'CameraGetBlackLevel', bounds=(0, 255))
    add('white_level', '白电平', '色彩与亮度', 'CameraGetWhiteLevel', bounds=(0, 255))
    try:
        if cap.sIspCapacity.bMonoSensor:
            raise ValueError('黑白相机没有 RGB 白平衡')
        rgb = get(s.CameraGetGain, h)
        for index, channel in enumerate('RGB'):
            fields['rgb_'+channel] = control(channel+' 数字增益（100 = 1×）', '色彩与亮度', rgb[index],
                                             minimum=getattr(cap.sRgbGainRange, 'i'+channel+'GainMin'),
                                             maximum=getattr(cap.sRgbGainRange, 'i'+channel+'GainMax'))
    except Exception as exc:
        for channel in 'RGB':
            fields['rgb_'+channel] = control(channel+' 数字增益', '色彩与亮度', error=str(exc))
    for key, label, getter in (('denoise_2d', '2D 降噪', 'CameraGetNoiseFilterState'),
                               ('dead_pixel', '坏点校正', 'CameraGetCorrectDeadPixel'),
                               ('flat_field', '使用已有平场校正', 'CameraFlatFieldingCorrectGetEnable')):
        add(key, label, '处理与速度', getter, 'bool')
    try:
        enabled, count, weighted, weights = get(s.CameraGetDenoise3DParams, h)
        fields['denoise_3d'] = control('3D 时域降噪', '处理与速度', bool(enabled), 'bool')
        fields['denoise_count'] = control('3D 降噪帧数', '处理与速度', max(2, count), minimum=2, maximum=8)
    except Exception as exc:
        fields['denoise_3d'] = control('3D 时域降噪', '处理与速度', kind='bool', error=str(exc))
        fields['denoise_count'] = control('3D 降噪帧数', '处理与速度', error=str(exc))
    speeds = [(cap.pFrameSpeedDesc[i].iIndex, cap.pFrameSpeedDesc[i].GetDescription()) for i in range(cap.iFrameSpeedDesc)]
    add('frame_speed', '相机采集速度档位', '处理与速度', 'CameraGetFrameSpeed', 'choice', choices=speeds,
        applicable=bool(speeds))
    try:
        processor = get(s.CameraGetIspProcessor, h)
        desc, count = ((cap.pBayerDecAlmSwDesc, cap.iBayerDecAlmSwDesc) if processor == 0 else
                       (cap.pBayerDecAlmHdDesc, cap.iBayerDecAlmHdDesc))
        choices = [(desc[i].iIndex, desc[i].GetDescription()) for i in range(count)]
        add('bayer_algorithm', 'Bayer 转彩色算法', '处理与速度', 'CameraGetBayerDecAlgorithm', 'choice',
            choices=choices, args=(processor,), applicable=bool(choices) and not cap.sIspCapacity.bMonoSensor)
    except Exception as exc:
        fields['bayer_algorithm'] = control('Bayer 转彩色算法', '处理与速度', error=str(exc))
    return fields


def validate_changes(fields, changes):
    for key, value in changes.items():
        if key not in fields or not fields[key]['supported']:
            raise ValueError(f'参数 {key} 不支持或无法读取')
        spec = fields[key]
        if spec['kind'] == 'bool' and type(value) is not bool:
            raise ValueError(f'{spec["label"]} 必须为开关值')
        if spec['kind'] in ('int', 'float'):
            import math
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{spec["label"]} 必须为有效数字')
            if spec['kind'] == 'int' and int(value) != value:
                raise ValueError(f'{spec["label"]} 必须为整数')
            if spec['min'] is not None and not spec['min'] <= value <= spec['max']:
                raise ValueError(f'{spec["label"]} 范围为 {spec["min"]}–{spec["max"]}')
        if spec['kind'] == 'choice' and value not in [choice[0] for choice in spec['choices']]:
            raise ValueError(f'{spec["label"]} 不是可用选项')
    merged = {key: spec['value'] for key, spec in fields.items()}
    merged.update(changes)
    if ('gamma' in changes or 'contrast' in changes) and merged.get('lut_mode') != 0:
        raise ValueError('调整 Gamma / 对比度前，请选择动态 LUT 模式')
    if 'lut_preset' in changes and merged.get('lut_mode') != 1:
        raise ValueError('选择预设曲线前，请切换到相机预设 LUT 模式')
    if {'black_level', 'white_level'} & changes.keys():
        if merged.get('black_level') is None or merged.get('white_level') is None or merged['black_level'] >= merged['white_level']:
            raise ValueError('黑电平必须小于白电平')
    return merged


def apply_sdk_controls(camera, changes):
    from multi_camera_core import Settings
    fields = sdk_controls(camera)
    merged = validate_changes(fields, changes)
    check, get, s, h = camera.sys.check, camera.sys.get, camera.s, camera.h
    backup = camera.temp.name+'/advanced-before.config'
    old = camera.settings()
    old_h, old_v = camera.mirror_horizontal, camera.mirror_vertical
    check(s.CameraSaveParameterToFile(h, backup))
    try:
        if {'exposure_ms', 'gain', 'rotate180'} & changes.keys():
            camera.apply(Settings(merged['exposure_ms'], merged['gain'], merged['rotate180']))
        setters = dict(lut_mode='CameraSetLutMode', lut_preset='CameraSelectLutPreset', gamma='CameraSetGamma',
                       contrast='CameraSetContrast', saturation='CameraSetSaturation', sharpness='CameraSetSharpness',
                       black_level='CameraSetBlackLevel', white_level='CameraSetWhiteLevel',
                       denoise_2d='CameraSetNoiseFilter', dead_pixel='CameraSetCorrectDeadPixel',
                       flat_field='CameraFlatFieldingCorrectSetEnable', frame_speed='CameraSetFrameSpeed')
        if {'black_level', 'white_level'} <= changes.keys() and changes['black_level'] >= fields['white_level']['value']:
            check(s.CameraSetWhiteLevel(h, int(changes['white_level'])))
        for key, setter in setters.items():
            if key in changes:
                if key == 'flat_field' and changes[key]:
                    valid, _ = get(s.CameraFlatFieldingCorrectGetParameterState, h)
                    if not valid:
                        raise ValueError('尚未加载有效的平场标定参数，不能启用')
                check(getattr(s, setter)(h, int(changes[key])))
        if any('rgb_'+channel in changes for channel in 'RGB'):
            check(s.CameraSetWbMode(h, 0))
            check(s.CameraSetGain(h, *(int(merged['rgb_'+c]) for c in 'RGB')))
        if {'denoise_3d', 'denoise_count'} & changes.keys():
            # A new frame count uses equal weights; this is stated in the panel.
            check(s.CameraSetDenoise3DParams(h, int(merged['denoise_3d']), int(merged['denoise_count']), None))
        if 'bayer_algorithm' in changes:
            processor = get(s.CameraGetIspProcessor, h)
            check(s.CameraSetBayerDecAlgorithm(h, processor, int(changes['bayer_algorithm'])))
        camera.mirror_horizontal = merged['mirror_horizontal']
        camera.mirror_vertical = merged['mirror_vertical']
        return sdk_controls(camera)
    except Exception as exc:
        try:
            check(s.CameraReadParameterFromFile(h, backup))
            camera.view_rotate180 = old.rotate180
            camera.mirror_horizontal, camera.mirror_vertical = old_h, old_v
        except Exception as rollback:
            raise RuntimeError(f'应用失败：{exc}；参数回滚也失败：{rollback}。请断开后检查配置') from exc
        raise


def demo_controls(camera):
    current = camera.settings()
    fields = dict(
        exposure_ms=control('曝光时间（ms）', '曝光与方向', current.exposure_ms, 'float', 0.01, 250),
        gain=control('模拟增益（倍）', '曝光与方向', current.gain, 'float', 1, 8),
        rotate180=control('画面旋转', '曝光与方向', current.rotate180, 'choice', choices=[(False, '0°'), (True, '180°')]),
        mirror_horizontal=control('水平镜像', '曝光与方向', camera.mirror_horizontal, 'bool'),
        mirror_vertical=control('垂直镜像', '曝光与方向', camera.mirror_vertical, 'bool'))
    defaults = dict(lut_mode=0, gamma=100, contrast=100, saturation=100, black_level=0, white_level=255,
                    rgb_R=100, rgb_G=100, rgb_B=100, sharpness=0, denoise_2d=False, denoise_3d=False, denoise_count=2)
    for key, default in defaults.items():
        value = camera.extra_controls.get(key, default)
        labels = dict(lut_mode='LUT 曲线模式', gamma='Gamma（SDK 整数）', contrast='对比度', saturation='饱和度',
                      black_level='黑电平', white_level='白电平', sharpness='锐化', denoise_2d='2D 降噪',
                      denoise_3d='3D 时域降噪', denoise_count='3D 降噪帧数')
        group = '处理与速度' if key in ('sharpness', 'denoise_2d', 'denoise_3d', 'denoise_count') else '色彩与亮度'
        fields[key] = control(labels.get(key, key.replace('rgb_', '')+' 数字增益（100 = 1×）'), group, value,
                              'choice' if key == 'lut_mode' else 'bool' if type(default) is bool else 'int',
                              None if key == 'lut_mode' or type(default) is bool else 2 if key == 'denoise_count' else 0,
                              None if key == 'lut_mode' or type(default) is bool else 8 if key == 'denoise_count' else 255,
                              choices=[(0, '动态（Gamma / 对比度）'), (1, '相机预设曲线'), (2, '已加载的自定义曲线')] if key == 'lut_mode' else None)
    fields['flat_field'] = control('使用已有平场校正', '处理与速度', kind='bool', error='模拟相机没有标定文件')
    return fields
