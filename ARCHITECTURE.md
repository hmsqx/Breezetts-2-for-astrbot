# Breeze 插件开发结构

`main.py` 只负责 AstrBot 注册、生命周期、服务控制器装配和薄工具入口。各模块通过同一个插件实例共享任务状态，不各建任务队列、配置副本或服务进程。

## 模块职责

| 模块 | 负责什么 |
| --- | --- |
| `main.py` | 工具/事件的装饰器入口、17条Web路由、初始化与卸载 |
| `settings.py` | 唯一配置文件、schema默认值、校验、revision、持久化 |
| `tts_provider.py` | 普通语音Provider、克隆请求、PCM/WAV完整性、临时音频清理 |
| `task_state.py` | 共享任务所有权、进度与暂时暂停TTS状态 |
| `dashboard.py` | 管理台鉴权、配置表单、服务启停 |
| `library.py` | 网络收录、本地上传、歌库列表与明确删除 |
| `cover.py` | 整首/片段范围、在线规划、转换请求、结果处理 |
| `voice_delivery.py` | FFmpeg正规化、完整PCM切分、顺序发送与部分失败 |
| `voice_parts.py` | 不依赖框架的自然停顿切点算法 |
| `chorus_planner.py` | 歌词来源、录音身份匹配、模型选段及时间轴校验 |
| `backend_client.py` | 翻唱API错误正文解析 |
| `service_control.py` | 本机进程身份、Windows Job、启动/关闭与归属 |
| `installer.py` | 明确请求后补齐本机依赖与模型、下载进度、安装进程与取消清理 |
| `runtime/`、`bundle_runtime.py` | 随插件携带的配套后端源码；后端修改后运行打包脚本刷新，只包含代码/许可证/运行配置，不含权重、歌库、音色或凭据 |
| `views/service/` | 原生管理页，使用同一官方配置和已鉴权API |

依赖方向是入口→业务模块→设置/纯算法。业务模块不得反向导入`main.py`。Mixins不各写`__init__`，由入口显式创建共享状态，`Star`初始化仍走完整MRO。

后台位于开发环境的配套服务目录：`server.py`负责HTTP接口和收录/转换编排，`transcribe_song.py`为短期CPU识别子进程，`public_proxy.py`限制公开来源访问，`yt_dlp_plugins/`处理下载器兼容，`seed-vc/`为推理实现。

## 添加一个工具

1. 将实现放入对应业务模块，保持未装饰方法。
2. 在`main.py`添加带`@llm_tool`的薄入口，声明参数与用途后转调实现。
3. 原生Web功能在`WEB_ROUTES`登记，并在所属模块执行管理台鉴权、任务互斥和输入校验。
4. 在对应测试中直接加载业务模块；不要通过AST抽取入口代码。

AstrBot按`handler.__module__`绑定工具和事件，所以`@llm_tool`、`@filter`保留在入口。搬走装饰器会使工具可能未绑定实例或热重载遗留。

## 保持的边界

- 安装复用共享任务槽阻止歌曲任务、配置保存与启停冲突；后台子进程由现有Windows Job托管。页面刷新不取消安装，插件卸载/重载会取消并回收子进程。文件校验不加载模型，不自动启动服务。
- `full/clip/learn`表示任务范围或种类，`voice/file`只表示投递方式；整首语音仍是`full`。
- 收录、上传、删除、识别和翻唱共享任务槽；后台还保留实际并发锁和源目录身份检查。
- 语音按完整PCM连续切分，切点只能来自可靠停顿。找不到安全路径时在发送前失败，不暗中切字或改发文件。
- 原始歌库只手动删除；临时成品按既有规则清理。
- 保存配置只影响后续请求，不自动启动模型。调试信息只在管理台/日志。
- 发送部分失败不重发已确认分段；取消任务必须释放自己的进程和临时资源。

## 测试

`test_support.py`提供隔离的`ASTRBOT_ROOT`和同包真实加载器：`load_plugin()`载入入口，`load_module(name)`载入业务模块。Mock实际依赖所在模块，例如`settings._load_plugin_config`、`cover.httpx`，不要只patch入口的旧别名。

插件测试使用现有AstrBot实例venv，逐项运行：

```powershell
python test_config.py
python test_config_api.py
python test_audio.py
python test_singing.py
python test_chorus_planner.py
python test_service_web.py
python test_installer.py
python test_voice_delivery.py
node plugin/test_service_view.cjs
```

后台测试用Cover的venv，在其根目录运行，临时根须使用新建独立目录：

```powershell
python -m pytest test_timeline.py test_server.py test_song_upload.py test_song_delete.py test_voice_parts.py -q --basetemp <全新临时目录>
```

Windows的asyncio初始化需要本机回环socketpair；受限环境若卡在事件循环初始化，先检查运行权限，勿将其当业务死锁。


真实模型与聊天投递验证应在独立环境进行；测试脚本及报告不包含在发布包中。源码测试需要 AstrBot 开发依赖，可通过 ASTRBOT_SOURCE 指定源码根目录。
