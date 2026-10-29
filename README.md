# MVSDK Python Library

## 多相机图形录像界面

### 迁移到另一台电脑

1. 在本机运行 `install\make_package.py`，在 `..\dist` 生成约 33 MB 的压缩包。压缩包包含：
   - 程序源码与文档
   - 离线 Python 依赖包（Pillow、numpy、Cython、pyzed）
   - `安装.bat` 和 `安装说明.txt`
2. 不打包的内容：
   - Python 环境（在新电脑上重建）
   - 录像和测试数据
   - FFmpeg、NVIDIA 驱动、MindVision SDK、ZED SDK 5.5、Python 3.14：这些在新电脑上按 `安装说明.txt` 单独安装
3. 在新电脑上解压，按说明装好上面几项后，双击 `安装.bat`。它会：
   - 检查环境
   - 离线创建 `.venv-recorder` 和 `.venv-zed`
   - 创建桌面快捷方式
   - 运行离线自检，结果写入 `install\install-report.txt`

FFmpeg 的查找顺序：环境变量 `FFMPEG_DIR` → 程序目录或上一级的 `ffmpegin` → 系统 PATH → 本机原来的 `D:\Program Files (x86)fmpegin`。

2.4 新界面：
- 左栏按“相机 → 录制设置 → 实验信息”顺序操作，最下方是开始 / 停止按钮（F9）。
- 中间自动排列全部相机画面；点画面在右侧打开参数面板。
- 顶栏显示录像状态、相机概况和磁盘空间；每批结束后弹出结果汇总。
- 支持高 DPI 屏幕。

每批录像中每台相机有自己的子文件夹。ZED 录完后会在后台自动导出校正后的左右目 MP4、逐帧时间戳 CSV 和导出记录（`zed_export.py`）；深度视频可选 NEURAL / NEURAL_LIGHT，默认不计算深度。

2.4 接入 ZED 2i（ZED SDK 5.5.0）。ZED 在独立进程中运行，使用 `.venv-zed` 环境，与 MindVision 一起一键开始和停止，录成 SVO2（默认 H.264 有损）。每次录像另存：
- 逐帧时间戳
- 标定文件副本
- 参数快照
- 完整性报告：相机缺帧、程序缺帧、重复帧分开统计

每台 MindVision 和 ZED 各在独立进程中运行（`mv_worker.py`、`zed_worker.py`，共用 `subprocess_worker.py`）。相机进程以较高优先级运行并关闭 Windows 节能降速，避免被放到能效核上。两台 MindVision 加一台 ZED 同时录 60 FPS MP4 60 秒，两次实测补帧和 SDK 丢帧均为 0。多台 ZED 和长时间录制尚未验证。设计与实测记录见 [ZED_集成方案.md](ZED_集成方案.md)。

2.3 实际探测并优先使用 NVIDIA NVENC，逐帧元数据异步写盘，方向变换移出取帧线程；编码队列满时停止报错，防止编码等待拖慢采集。单台 MV-SUA202GC、1920×1200、13 ms 实机短测中，30/60 FPS 均未重复补帧；长期与多机负载仍需验证。重开程序后标题为 2.3。

2.2 优化默认方向变换，将预览处理移出采集线程，并记录采集/编码阶段耗时以定位实机瓶颈。界面 FPS 标为应用“接收”速度；MP4 重复帧计数不等于硬件丢帧数。原有时间戳选帧策略保留。

每路预览旁提供“放大对焦”（也可双击预览），从原始分辨率画面显示实时细节；支持适应窗口、100%/200%/400%、拖动查看与 F11 全屏，不改变录制内容。

2.1 新增按设备能力读取的高级参数面板，包含方向、LUT/Gamma、RGB 增益、黑白电平、降噪等。主界面的旧旋转控件已移除，预览与 MP4 默认旋转 180° 并开启水平镜像；在高级面板改变方向会立即应用。研究 RAW 保持原始字节，应用层方向单独记录于元数据。未加入 ROI 控制。

2.0 新增研究 RAW 全帧模式、逐帧元数据、实验信息、SDK 参数预设和完整性报告。格式与校验方法见 [RESEARCH_DATA_格式说明.txt](RESEARCH_DATA_格式说明.txt)。研究 RAW 模式保存全部接收帧，30/60 FPS 仅适用于 MP4 模式；程序更新后需关闭旧窗口再重新打开。

双击 `Y:\多相机录像台.lnk` 启动。支持勾选相机、实时预览、独立曝光/增益/一次白平衡/180° 旋转，以及统一开始和停止 30/60 FPS MP4 录像。每次录像建立独立批次目录，按相机序列号保存视频及采集统计。

使用前在官方软件中断开相机。完整说明见 [RECORDER_使用说明.txt](RECORDER_使用说明.txt)。模拟体验入口为本目录的 `模拟演示.lnk`，不会连接实际设备。实际多机带宽和颜色表现尚待实机验证。

界面与采集代码：`multi_camera_app.py`、`multi_camera_core.py`；高级控制：`advanced_panel.py`、`camera_controls.py`。独立运行环境为 `.venv-recorder`。离线验证：

```powershell
& '.\.venv-recorder\Scripts\python.exe' -m unittest test_capture_performance test_advanced_controls test_research_data test_multi_camera test_record_video test_zed test_process_worker test_export -v
& '.\.venv-recorder\Scripts\python.exe' gui_smoke_test.py
```

## MV-SUA202GC 使用核查

针对本机相机、MindVision 2.1.10.197、官方开发手册与既有 RAW 录像的中文核查记录见 [USAGE_ZH.md](USAGE_ZH.md)。其中明确区分官方规格、历史实测、磁盘配置与尚未验证的硬件功能。

### 30 / 60 FPS 视频录制

`record_video.py` 使用 SDK ISP 处理彩色画面，以帧头时间戳选择输出帧，再由 FFmpeg 编码成 H.264 MP4。无需 numpy/OpenCV。目前通过了离线时间轴与真实 FFmpeg 编码测试，尚未进行该脚本的相机实录验收。

运行前关闭其他使用相机的软件。下面示例使用本机实际的 Python 路径（当前 PATH 中的 `python` 是 Windows 应用别名）：

```powershell
& 'D:\Datas\env\miniforge\python.exe' 'Y:\camera-sdk-review\mvsdk-python\record_video.py' --fps 30 --seconds 10 --output 'Y:\test\capture_30fps.mp4'
& 'D:\Datas\env\miniforge\python.exe' 'Y:\camera-sdk-review\mvsdk-python\record_video.py' --fps 60 --seconds 10 --output 'Y:\test\capture_60fps.mp4'
```

- 默认曝光：30 FPS 使用 20 ms；60 FPS 使用 10 ms。可通过 `--exposure-ms` 设置，必须小于一帧时长。
- 沿用加载配置的分辨率和增益；`--gain 2` 表示 2 倍模拟增益，`--rotate180` 设置 180 度旋转。
- `--config 'D:\path\camera.config'` 加载 SDK 参数文件。要先在相同 SDK 图像处理流程中调好白平衡和色彩；录像导出的 JSON 不是这个参数文件，也不会自动复用之前离线 MP4 的手工校色。
- `--serial` 默认绑定此前使用的相机序列号 046011323028。
- Ctrl+C 可提前结束并封装已有帧；程序提供开始前配置备份和结束时恢复，恢复失败会写入错误报告。
- 输出旁的 `.json` 记录实际接收速度、帧数、重复帧、SDK 统计和错误；`.frames.csv` 记录每个输出帧对应的相机时间戳。
- 要确认真正的 60 FPS 视频，检查 `complete=true`、`repeated_frames=0`、无错误，再观察画质与时间戳。不能只看 MP4 的 FPS 标签。
- 输出文件及旁文件存在时拒绝覆盖，请换新的文件名。软件缩放需先关闭；使用当前配置的偶数宽高。

离线测试：`D:\Datas\env\miniforge\python.exe -m unittest test_record_video -v`（在本目录运行）。测试不会打开相机。

## Get started

### Windows

- Install the mvsdk via the official installer
- `pip install .`

### Linux

- `pip install .`
- Launch a Python terminal with **sudo/root privileges**, run:

    ```python
    from mvsdk.utils import install_driver
    install_driver()
    ```

  **Note:** Driver installation requires root privileges to write USB rules to `/etc/udev/rules.d/`
  
  After installation, you may need to restart the system or reload udev rules:
  ```bash
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```

## How to remove

### Windows

- Remove with the official uninstaller

### Linux

- **First**, launch a Python terminal with **sudo/root privileges**, run:

    ```python
    from mvsdk.utils import uninstall_driver
    uninstall_driver()
    ```

  **Note:** Driver uninstallation requires root privileges to remove USB rules from `/etc/udev/rules.d/`

- **Then**, uninstall the package:
  ```bash
  pip uninstall mvsdk
  ```

## Development Installation

For development, you can install in editable mode:

```bash
pip install -e .
```

## Building Distribution

To build the package for distribution:

```bash
pip install build
python -m build
```

This will create distribution files in the `dist/` directory.

## How to use

Refer to the official guide.
