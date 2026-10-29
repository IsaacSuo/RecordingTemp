# MV-SUA202GC：SDK 核查与正确使用

核查日期：2026-10-04。对象是这台 MV-SUA202GC、用户提供的 Python 包以及本机 MindVision 2.1.10.197。

本次只读取文件、手册和动态库版本/导出符号，没有枚举、打开或控制相机。下文实机结果来自 2026-10-02；配置文件是历史保存值，不代表相机此刻正在使用的值。

## 1. 结论与证据范围

| 项目 | 已确认的内容 | 证据 |
|---|---|---|
| 型号/接口 | MV-SUA202GC，USB3.0，彩色 | 前次设备枚举、官网 |
| 最大图像 | 1920×1200 | 官网、前次实际取图 |
| 全尺寸额定速度 | 最高 165.5 FPS | 官网；前次 RAW 接收测试约 166 FPS |
| 曝光范围 | 官网列 0.0039～256.1 ms | 实际范围、步长仍应从 SDK 查询 |
| 模拟增益 | 前次设备报告整数 8～64，步长系数 0.125，即 1～8 倍 | 前次 capability 与帧头读回 |
| 原始颜色格式 | 本次录像是 Bayer RGGB、8 bit、每行 1920 字节 | 录像 JSON；官网列 Bayer RG 8bit |
| A/D 与文件位深 | 官网另列 A/D 10 bit；不能据此宣称已保存 10 bit | 本次 RAW 实际只有 8 bit |
| 速度档位 | 前次枚举 0=Low、1=Normal | 不能传入 166 当作档位 |
| ROI、硬件 ISP、外触发 | 通用 SDK 有相关接口；本机具体支持需读 capability 并实测 | 不用“SDK 有函数”代替设备支持证据 |

官网：https://www.mindvision.com.cn/archives/1460

**这台是百帧级工业相机。全分辨率时设置再短的曝光，也不能保证超过硬件额定帧率。**

## 2. 实际有四个层次

1. 镜头、照明和传感器：决定进光、焦点、运动模糊、原始信噪比。
2. Windows 驱动和原生 SDK：枚举、控制相机、接收 RAW、缓冲区管理。
3. 图像处理 ISP：Bayer 转彩色、白平衡、颜色矩阵、Gamma/LUT、降噪、方向变换等。
4. 录像程序：缓存、触发前后帧选择、写盘、视频编码和播放速度。

你给的 `mvsdk` 是 ctypes 接口，不是驱动，也不是完整录像软件。Python 主要负责调用动态库。

本机本次实际加载的库是 `C:\Windows\SYSTEM32\MVCAMSDK_X64.DLL`，版本 `2.1.10.197`。安装资料在 `D:\Program Files (x86)\MindVision`。

导出 JSON 的 producer 标识为 `MvSnapCapture 1.22.0`；不能把该程序的所有字段和功能等同于通用 SDK 或 MVDCP2。

## 3. Python 包与官方示例的差异

静态 AST 对比：附件模块有 309 个顶层函数，安装包 Basic 示例的 `mvsdk.py` 有 317 个。共有函数只有 `_Init` 的实现不同，共有结构体定义一致。官方版本另有 `tSdkFrameEvent`。

官方多出的函数：

- `CameraSetAnalogGainX`、`CameraGetAnalogGainX`、`CameraGetAnalogGainXRange`
- `CameraGetTransferRoiEnable`
- `CameraSetExtTrigIntervalTime`、`CameraGetExtTrigIntervalTime`
- `CameraSetFrameEventCallback`
- `CameraSetDataDirectory`

原生头文件还有两份 Python 都没有包装的函数。因此 Python 文件不是完整 API 清单。

本次只检查 DLL 导出符号：GainX、FrameEventCallback、SetFrameRate 存在；`CameraSetDataDirectory` 不存在。这说明**连安装包里的示例也不能不加检查地直接使用**。DLL 有符号也不保证这台硬件支持。

以后以本机 `CameraApi.h`、`CameraDefine.H` 和设备 capability 三者为准；需要缺失接口时，按原型补充明确的 ctypes 类型并检查返回码。无需为了“升级 SDK”从 PyPI 随意安装同名包。

## 4. 打开相机和参数加载

标准顺序：

```text
CameraSdkInit
→ CameraEnumerateDevice（按序列号选择）
→ CameraIsOpened（辅助检查，仍需处理打开时的竞争）
→ CameraInit
→ CameraGetCapability
→ 读取/加载明确的参数配置
→ 设置参数并读回确认
→ CameraPlay
→ 循环 GetImageBuffer → 处理/复制 → ReleaseImageBuffer
→ 停止取图线程，结束录像
→ CameraUnInit，释放自有内存
```

官方软件与 Python 应轮流控制同一台相机。SDK 定义了 `-18`（设备已打开）和 `-45`（禁止访问/被占用）等状态。**黑画面本身不能证明“被官方软件占用导致没信号”**，要检查打开结果、帧头、图像数据和错误码。

`CameraInit(info, -1, -1)` 意味着沿用上次参数加载方式和参数组，不意味着保持官方软件当前内存状态，也不意味着每次恢复工厂默认。

参数加载方式（原生枚举）：0 按型号、1 按昵称、2 按序列号、3 从设备非易失存储加载。方式 3 需 `cap.bParamInDevice` 支持。A/B/C/D 对应参数组 0/1/2/3。

建议为这台相机按序列号管理配置，先在演示软件调好并导出独立 `.config`，再由 Python 使用 `CameraReadParameterFromFile` 加载。实验前用 `CameraSaveParameterToFile` 备份；明确区分临时调参、恢复备份、保存为新配置。官方界面还可能启用了关闭相机自动保存，不能假设关闭就自动恢复。

找到的历史文件 `D:\Program Files (x86)\MindVision\Camera\Configs\046011323028-Group0.config` 保存于 2026-10-02 03:43:44，内容包括：手动曝光 20000 μs、analog_gain=32（依前次步长约 4 倍）、Normal 档、RGB 数字增益均为 100、Gamma=100、连续采集。这些是**磁盘保存值**。

## 5. 曝光、增益、Gamma：三种不同作用

| 控制 | 接口/单位 | 正确解释 |
|---|---|---|
| 曝光 | `CameraSetExposureTime(h, us)` | 20000 是 20 ms；设置后必须用 GetExposureTime 读回，传感器有行时间量化 |
| 自动曝光 | `CameraSetAeState(h, 0/1)` | 自动模式可能调整曝光和增益；固定实验通常先关闭自动 |
| 自动曝光范围 | `CameraSetAeExposureRange`，μs | 允许自动调光时，限制最大曝光，避免帧率一路下降 |
| 自动增益范围 | `CameraSetAeAnalogGainRange`，SDK 整数单位 | 按 capability 换算，不能直接传倍数 |
| 模拟增益 | `CameraSetAnalogGain(h, value)` | 本机前次为 value×0.125 倍：8=1×，16=2×，32=4×，64=8× |
| 倍数增益接口 | 新版 `CameraSetAnalogGainX(h, float)` | 单位直接是倍数；附件没有包装，原生 DLL 有导出 |
| RGB 数字增益 | `CameraSetGain(h, R,G,B)` | 各通道数值÷100 为放大倍数；用于颜色处理，不是模拟增益 |
| Gamma | `CameraSetGamma(h, int)` | 附件为整数接口；当前配置 100。不要直接传 JSON 的 0.56 或 UI 的 1.01 |

Gamma 只有 `CameraSetLutMode(h, 0)`（动态生成 LUT）时才按该参数生效；预设或自定义 LUT 模式不能靠改 Gamma 推断效果。准确曲线需核对当前 ISP，不能把它直接等同于 FFmpeg 的 Gamma 表达式。

曝光积累更多光，代价是运动模糊和可能降低帧率；增益放大已有信号，也放大噪声；Gamma 改显示层次，不能追回已经剪切的亮部或没记录到的暗部细节。增益单位不是标准摄影 ISO，不要直接换算成 ISO 800/1600。

## 6. 帧率、ROI 与带宽

`CameraSetFrameSpeed` 设置能力列表中的档位。本机前次只有 Low 和 Normal。原生 SDK 另有 `CameraSetFrameRate`，但头文件明确注明仅部分网口相机支持；不能承诺 USB3 的 SUA202GC 支持它。

全分辨率下可用下面的估算判断曝光限制（不是实际帧率保证）：

```text
FPS ≤ min(当前读出/传输上限, 1,000,000 / 曝光微秒数)
```

| 曝光 | 仅由曝光给出的上限 | 对本机全尺寸的意义 |
|---|---:|---|
| 20 ms | 50 FPS | 已有录像时间戳实测约 49.78 FPS |
| 10 ms | 100 FPS | 还需实测实际捕获与保存速度 |
| 5 ms | 200 FPS | 本机全尺寸仍受约 165.5 FPS 额定上限约束 |
| 1 ms | 1000 FPS | 不会因此变成千帧相机；前次 RAW 接收约 166 FPS |

前次 `camera_demo.py` 无预览模式直接取 RAW 后释放，没有跑全部 ISP、编码和磁盘写入。这是**接收速度测试，不是完整彩色视频录像性能测试**。

硬件 ROI 使用 `CameraSetImageResolution` / `CameraSetImageResolutionEx`。后者自定义索引为 `0xFF`，普通模式为 0，包含 x/y/width/height；ZoomWidth/Height 为 0 表示不缩放。先读能力、设置、再读回实际分辨率。偏移、尺寸和步进要满足设备限制，不能承诺任何宽高都能生效。

减少 ROI 可能提高帧率，但没有对这台做 ROI 速度扫描，不能按面积比例保证 2 倍/4 倍提升。软件 resize 和硬件 ROI 不同；软件缩图不会让传感器更快。

1920×1200 Bayer8：每帧 2,304,000 字节（2.304 MB）。按 165.5 FPS 计算，纯像素流约 **381.3 MB/s**，一分钟约 22.9 GB，未计文件头等开销。展开 RGB24 后约 **1,143.9 MB/s**。这说明高速保存会明显受到 USB 链路、内存复制、ISP、编码和磁盘持续写速影响。标称 USB 口速度不是实际可持续写盘速度。

要分别记录：传感器/SDK 有效帧数、应用拿到的帧数、预览帧数、写盘成功帧数、视频播放 FPS。预览只显示 30 FPS 不等于采集只有 30 FPS。

## 7. RAW 变成正常彩色图像

本次 RAW 是 RGGB 排列，每个像素只有一个颜色采样。把它直接当灰度图、RGB 或错误 Bayer 排列都会出问题。正确路径为：

```text
原始帧 + 原始帧头
→ 正确 Bayer 解码
→ 与录制时一致的 ISP/白平衡/色彩和亮度处理
→ RGB 或 BGR 图像
→ 显示、图片保存或视频编码
```

使用原生 SDK 时，优先让 `CameraImageProcess` 按明确加载的配置处理。Qt 等 RGB 消费端可选 `CAMERA_MEDIA_TYPE_RGB8`；OpenCV 通常用 `CAMERA_MEDIA_TYPE_BGR8`。`CameraSetIspOutFormat` 控制的是 ISP 输出格式，不是把传感器 USB 原始数据改为 RGB。

`CameraSetMediaType` 才是选择相机原始格式，但参数是能力列表中的**索引**，不是 `CAMERA_MEDIA_TYPE_...` 格式常量。必须读取 `cap.pMediaTypeDesc`。不能因为官网写 A/D 10 bit 就强行把现有 8 bit RAW 按 16 bit 读取。

`CameraImageProcess` 会修改传入的帧头，包括 uiMediaType。需要同时保存 RAW 与处理结果时，**先 clone 原始帧头**，保留原始格式/字节数/时间戳；处理后再按输出帧头解释 RGB 数据。

ISP 可能在 PC 或设备执行，用 `sIspCapacity` 与 `CameraGetIspProcessor` 判断。不能因为通用 SDK 有硬件 ISP 接口就宣称本机全套处理都在相机上完成。

### 白平衡

先让图像曝光合理、对好焦，将白纸或灰卡放在主体相同照明下，避免过曝、反光和全黑。必要时设置 `CameraSetWbWindow`，然后：

```python
check(sdk.CameraSetWbMode(h, 0))  # 手动/单次白平衡模式
check(sdk.CameraSetOnceWB(h))    # 下一帧开始生效；继续正常取图和 ISP 处理
```

稳定采集后读回相关颜色参数并保存配置。固定照明的实验通常使用校准后固定的白平衡，避免连续自动白平衡造成帧间颜色变化。

高增益下的偏紫可能涉及通道噪声、黑电平、白平衡等，不能仅凭肉眼确定是缺少红外截止滤镜。先用低增益、均匀照明、正确 Bayer/通道顺序和一次白平衡排查。

### 为什么已有 MP4 颜色和亮度不同

录像 JSON 标记 `softwareIspEnabled=false`、`isp.applied=false`、`save.appliedSoftwareIsp=false`。文件保存的是未应用该软件 ISP 的 RAW；JSON 另存处理配方，不等于配方已写入每个像素。

首版 FFmpeg 视频只解 Bayer，没有复现配方，因此偏绿、偏暗。提亮版使用了基于中性物体的近似白平衡、黑位和亮度曲线，**不是官方 ISP 的逐像素复现，也不是重新曝光**。

JSON 内的 V3 白平衡、校准 Gamma、颜色矩阵、前后黑位、处理顺序不能随意重排。本机 Python 封装没有与这份导出配方一一对应的完整接口。要严格匹配预览，最稳妥的是由同一程序使用同一 ISP 配方导出处理后帧，或取得该程序的完整处理算法。仅调用通用 `CameraImageProcess` 也不能保证重现另一个程序自己的 V3 管线。

不要把这次校色的 RGB 系数直接套用到所有新录像；光源、相机配置或场景变了，应重新校准。

## 8. 取图缓冲区与错误处理

`CameraGetImageBuffer` 返回 SDK 管理的指针，超时单位为毫秒。成功获取后，无论后续是否报错，都应在 finally 中调用 `CameraReleaseImageBuffer`。释放后不能继续使用该指针；交给其他线程前必须复制到自有内存。

典型片段（已有有效句柄、已分配足够的输出缓冲区）：

```python
def check(code):
    if code != sdk.CAMERA_STATUS_SUCCESS:
        raise sdk.CameraException(code)

raw, raw_head = sdk.CameraGetImageBuffer(h, 1000)
try:
    processed_head = raw_head.clone()
    check(sdk.CameraImageProcess(h, raw, output_buffer, processed_head))
    # 原始数据用 raw_head；处理结果用 processed_head。
    # 跨线程/跨循环保留结果时，还要复制 output_buffer 中的数据。
finally:
    check(sdk.CameraReleaseImageBuffer(h, raw))
```

附件不是所有函数都会抛异常。大量 Setter 直接返回状态码；许多 Getter 返回数值/结构体，但把错误放进线程局部 `GetLastError()`。必须紧跟调用检查错误，再使用返回值，不能把失败返回的默认 0 当成“零增益”“零帧率”。

回调必须保留 Python 回调对象的强引用，不可在回调中再次调用阻塞 GetImageBuffer（手册提示可能死锁）。回调内避免长时间编码、写盘或 GUI 操作。退出时先让工作线程停止并 join，再释放相机和输出内存。

`CameraGetImageBufferPriority(..., 1)` 是取最新帧，会丢弃更旧缓存，适合低延迟预览；连续完整录像应优先使用顺序取图/oldest（0），并有自己的队列和丢帧统计。不要拿预览模式测“完整保存”。

官方 Python Record 示例也只是示例：其停止等待循环、共享 QImage 底层内存和录像关闭流程需要审查，不能原样当生产代码使用。原生录像必须配对 `CameraInitRecord` / `CameraPushFrame` / `CameraStopRecord`。

## 9. 时间戳和录像播放速度

三个时间单位不同：

- `tSdkFrameHead.uiTimeStamp`：0.1 毫秒/计数；32 位，要处理回绕。
- `CameraGetFrameTimeStamp`：原生说明为微秒，Python 合并高低 32 位成 64 位。该设备是否支持及与帧绑定方式需读错误并验证。
- 本次导出 JSON 的 `timestampNs`：纳秒。

不要混用单位，也不要在另一个线程延迟查询“当前帧”时间戳后就假设它属于先前取得的图像。优先使用与帧同步保存的时间信息。

本次保存帧时间间隔约 20.088 ms，推得 49.7811 FPS。按相同 FPS 编码为原速；按 25 FPS 一帧不漏地播放，约为 1.99 倍慢放。

SDK 自带录像：`CameraInitRecord` 编码格式 0=未压缩、1=MSCV、4=H.264；`CameraPushFrame` 可使用帧头时间戳同步。iFrameRate 是录像参数，不能当作设置传感器速度的接口；也不能未经验证把它当“逐帧慢放开关”。头文件说明 2GB 自动分段选项未实现，不能依赖。

## 10. 本次 RAW 文件审计

该文件是 `MvSnapCapture` 的 `raw_multi` 容器实例，不是通用无头 RAW。观察到：

| 内容 | 本次值 |
|---|---|
| 魔数 | ASCII `MVSC` |
| 版本/头长 | 小端 1 / 32 字节 |
| 帧字节数/数量 | 2,304,000 / 674 |
| 文件长度 | 32 + 674×2,304,000 = 1,552,896,032 字节 |
| 像素布局 | RGGB8，1920×1200，stride=1920，top_down |
| 完整性 | SHA256 与 JSON 一致；已保存的 SDK 帧号连续 |
| 计划与实际 | 计划 1000，实际 674，complete=false |
| 保存范围 | hostFrameId 217～890；计划范围 217～1216 |

这说明保存下来的部分可一致读取，但不能证明计划的 1000 帧已录完。`lostFrameCount=0` 和 `complete=false` 可以同时成立：采集/接收丢帧和目标录制窗口未完成是不同问题。仅凭现有文件不能断定是磁盘、提前结束、缓存选择还是导出程序行为。

JSON 的 acquisition.gain=0 没有明确给出与 SDK 整数增益的映射；不能据此认定相机零倍增益。稍后的磁盘配置中 analog_gain=32，同样不能反向证明每个录制帧恰好是 4 倍。以后保存每帧曝光/增益、SDK 状态和配置快照，避免歧义。

现有 `Y:\test\convert_raw.py` 只支持已经检查过的单分段、紧密排列 Bayer8、32 字节 MVSC 文件头和基本均匀时间戳。不能拿去盲转别的 SDK RAW、10/12 bit、带行填充或多分段文件。

## 11. 触发、旋转与其他功能

相机采集触发：0 连续、1 软件、2 硬件（实际可用列表看 capability）。软件触发通常是设置模式 1、CameraPlay、CameraSoftTrigger，再取帧。硬件触发涉及信号类型、边沿、延时、曝光模式和接线，必须按此型号接线资料，不猜电压和引脚。

录制程序的“开始/中心/结束触发”通常是选择缓存中的录制窗口；它不等同于相机传感器的触发模式。你 JSON 的 center + software 不能单独证明传感器处于 SDK 模式 1。

180° 旋转可以对处理后的图像使用 `CameraSetRotate(h, 2)` 对应的 ISP 功能，或者 `CameraFlipFrameBuffer(buffer, head, 3)`。前者角度枚举 0/1/2/3 对应 0/90/180/270°；后者标志 1 是上下翻转、2 左右翻转、3 同时翻转。设置是否生效需看实际输出。

Windows 环境本身不是“所有图像必须上下翻转”的充分理由，应按具体接口的行顺序及显示端验证。给定 RAW 已标 top_down，且用户需要的是 180°。应先正确解 Bayer，再做显示方向变换，避免直接翻转 Bayer 后仍用原来的颜色排列。

SDK 还包含单次黑平衡、黑/白电平、坏点校正、平场、镜头畸变校正、锐化、2D/3D 降噪和 GPIO/闪光同步。它们有各自条件，不宜为了“画面更好”全部开启。3D 降噪使用多帧，会改变运动图像的时间特征，做运动测量时尤其要谨慎评估。

## 12. 实际使用顺序

1. 让一个程序控制相机；导出并备份当前参数，记录镜头、光源、相机安装方向。
2. 先用全尺寸、连续采集、低增益和适当曝光检查焦点与亮度；必要时用 10～20 ms 方便对焦。
3. 决定目标帧率和允许的运动模糊，再缩短曝光。不要把“能到 166 FPS”和“每帧都足够清晰”混为一谈。
4. 优先调整照明、光圈和合理曝光；再按噪声容忍度增加增益。现场光源可能有频闪，自动抗频闪又可能限制曝光；高速场景应评估稳定照明。
5. 对中性参考做一次白平衡，确认 ISP、LUT、输出 RGB/BGR 和方向；随后固定这些参数以保持帧间一致。
6. 做一次端到端录像测试：记录接收帧数、写入帧数、时间戳间隔、超时、SDK 丢帧、实际时长和磁盘速度。
7. RAW 路线保存原始帧头、完整元数据和处理配方；直接视频路线编码处理后的帧。两者都要验证结束时文件已正确关闭。
8. 保存为具名配置，例如 focus、capture_50fps、capture_fullspeed；名字是目标，实际 FPS 必须测量，不靠名字保证。

## 13. 对前面结论的修正

- 单纯增益不足、镜头盖、对焦等解释都不能代替帧数据和完整 ISP 检查；此前的暗画面不是唯一可归因于照明的证据。
- 模拟增益确实可调，不能只建议补光；但高增益噪声不是无损增亮。
- 自动状态、配置加载和软件 ISP 会影响实际结果；先前只恢复某个曝光数值不等于恢复完整设置。
- RAW 接收约 166 FPS 不等于已验证 166 FPS 彩色 MP4 连续录像。
- 提亮版只是近似校色，不是官方 ISP 等价实现。
- 录制未完成不等于传感器丢帧。
- `camera_demo.py` 仍定位为早期连通性示例，未实现完整配置恢复、生产级录像、全面能力检查；它的 Windows 翻转假设也应按实际显示端处理。

## 14. 尚未实机确认的项目

ROI 尺寸/对齐与最高速度表；实际 10 bit 输出可用性；硬件 ISP 的支持范围；外触发/闪光时序；目标磁盘持续录像上限；完整复现 MvSnapCapture V3 ISP；当前相机实时设置。以上未因本次阅读而变成“已验证”。

## 来源定位

- 官方型号页：https://www.mindvision.com.cn/archives/1460
- 开发手册：`D:\Program Files (x86)\MindVision\Document\MindVision工业相机开发手册.pdf`，开发流程印刷页 8～11、曝光与白平衡 26～28、参数保存 30～32。
- 演示软件手册：`D:\Program Files (x86)\MindVision\Document\MindVision演示软件用户手册.pdf`，模拟控制与 LUT/降噪第 25～26 页、颜色和白平衡第 26～28 页。
- 当前原生 API：`D:\Program Files (x86)\MindVision\Demo\VC++\Include\CameraApi.h`、`CameraDefine.H`、`CameraStatus.h`。
- 当前官方 Python 示例：`D:\Program Files (x86)\MindVision\Demo\Python\Basic`、`Advanced`、`Record`。
- 机器可读核查结果：`Y:\camera-sdk-review\audit\sdk_audit.json`。
- 录像 JSON：`Y:\test\recording_20261002_034330360_Camera MV-SUA202GC#0004-0002_001.json`。

手册是通用 SDK 说明，某个函数出现不代表本机支持；产品页、通用 API、导出程序元数据和前次实测分别说明不同层面的事实。
