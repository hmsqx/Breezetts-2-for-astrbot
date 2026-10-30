# 开发架构

## 目录边界

AstrBot 所需的 `main.py`、`metadata.yaml`、`_conf_schema.json` 留在根目录；自动发现的页面保留在 `views/service/`。实现、测试、脚本和文档分别放入自己的目录。

| 目录 / 文件 | 职责 |
| --- | --- |
| `main.py` | 保留框架装饰器，注册 8 个 LLM 工具与 19 条 Web 路由，装配服务及管理生命周期 |
| `breeze/settings.py` | 唯一配置入口，负责默认值、校验、revision 冲突检查和原子保存 |
| `breeze/task_state.py` | 共用歌曲任务状态、进度和暂时暂停 TTS 的状态 |
| `breeze/tts_provider.py` | 普通语音 Provider、参考音频和 PCM/WAV 校验、临时文件清理 |
| `breeze/dashboard.py` | 已鉴权的配置、服务和安装管理 API |
| `breeze/singing/library.py` | 联网收录、本地上传、查询与明确删除原始音源 |
| `breeze/singing/cover.py` | 整首 / 片段编排、自动高潮、转换请求与结果处理 |
| `breeze/singing/chorus_planner.py` | 歌词查询、版本匹配、模型选段及时间轴校验 |
| `breeze/singing/voice_delivery.py` | 正规化音频、完整 PCM 分段、顺序投递与部分失败处理 |
| `breeze/singing/voice_parts.py` | 无框架依赖的自然停顿切点算法 |
| `breeze/singing/backend_client.py` | 配套后端错误正文解析 |
| `breeze/services/service_control.py` | 本机进程身份、Windows Job、启停及进程归属 |
| `breeze/services/installer.py` | 明确请求后安装依赖与模型，处理换源、进度及取消 |
| `runtime/tts/`、`runtime/cover/` | 安装时复制到服务目录的后端源码，不是插件本身的业务模块 |
| `runtime/seed-vc.zip` | 固定版本、带完整校验值的上游代码和许可证 |
| `views/service/` | 管理页 HTML、JavaScript 与 CSS |
| `tests/` | 真实模块的隔离回归与页面交互检查 |
| `scripts/` | 开发维护用打包工具 |
| `docs/` | 架构与版本记录 |

业务模块使用相对导入，不反向导入 `main.py`。Mixins 不各建 `__init__`、配置副本或任务队列，入口显式创建共用状态并保留完整 MRO。

## 资源位置

移动 Python 模块不会移动用户配置与服务数据：

- `breeze.settings.PLUGIN_ROOT` 指向插件根目录，用于查找配置 schema 和旧版配置迁移入口。
- `breeze.services.installer.BUNDLED` 指向插件根目录的 `runtime/`，独立安装子进程同样使用该位置。
- 正式配置仍由 AstrBot 写入 `data/config/astrbot_plugin_breeze_tts_config.json`。
- 原始歌库和服务目录仍由原有服务控制器管理。

## 添加功能

1. 在对应业务目录实现逻辑；普通语音放在 `tts_provider.py`，歌曲相关放在 `singing/`，进程管理放在 `services/`。
2. 在根目录 `main.py` 增加薄工具 / 事件入口，保留 `@llm_tool`、`@filter` 装饰器。
3. Web 路由在 `main.py` 的 `WEB_ROUTES` 登记，由对应模块验证管理台身份、请求及任务状态。
4. 使用 `tests/test_support.py` 的真实模块加载器验证调用，不通过 AST 抽取生产代码。

AstrBot 按处理器的 `__module__` 绑定注册入口，因此框架装饰器留在 `main.py`。新增处理器后应验证真实注册结果及插件卸载清理。

## 任务、进程与数据

- 收录、上传、删除、识别、翻唱及安装复用原有任务互斥；后端保留并发锁。
- `full / clip / learn` 表示任务范围或类型；`voice / file` 只表示投递方式。
- 语音分段在发送前验证完整切点。无安全路径时失败，不截断唱词或自动改成文件。
- 原始音源只手动删除；临时成品按清理策略处理。
- 已确认投递的分段不会因后续失败重复发送。
- 取消任务需要释放自己的进程和临时资源。
- 安装及服务进程保留生命周期归属；配置保存不自动启动模型。
- 诊断细节保留在管理页和日志中。

## 测试

在插件根目录执行：

```powershell
$env:ASTRBOT_SOURCE = "C:/path/to/AstrBot"
python -m unittest discover -s tests -p "test_*.py" -q
python tests/test_service_web.py
node tests/test_service_view.cjs
```

`ASTRBOT_SOURCE` 用于 AstrBot 以源码形式运行的开发环境；已可正常导入框架时可以不设置。请使用安装了 AstrBot 开发依赖的 Python。

`test_support.py` 建立隔离的 `ASTRBOT_ROOT`，再导入同一份真实业务模块。例如 `load_module("singing.cover")`、`load_module("services.installer")`、`load_plugin()`。Mock 模块实际依赖的位置，如 `settings._load_plugin_config` 或 `singing.cover.httpx`。

语音投递回归使用 FFmpeg 和临时 PCM 文件。测试不会生成真实歌曲或向 QQ 投递。Windows 的 asyncio 初始化使用本机 socketpair，运行时需要允许本机回环通信。

## 更新配套源码

`scripts/bundle_runtime.py` 默认核对仓库中已有的后端载荷，可显式指定外部开发源码目录：

```powershell
python scripts/bundle_runtime.py --cover-source "D:/dev/breeze-cover" --tts-source "D:/dev/breeze-tts"
```

更新 Seed-VC 随附源码时，额外提供固定提交的原始 ZIP：

```powershell
python scripts/bundle_runtime.py "D:/downloads/seed-vc-upstream.zip"
```

脚本先核对固定 SHA-256，只收录代码、配置和原始许可，不打包示例音频、模型权重、歌库或凭据。修改配套源码后需刷新载荷，并核对前后端接口。
