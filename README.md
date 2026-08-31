# Bilibili 内容解析插件

为 MaiBot 提供 Bilibili 内容自动解析和视频搜索能力。插件收到消息后会识别其中的 Bilibili 链接或内容编号，调用 Bilibili Web API 获取信息，并向原聊天流发送解析结果。

插件 ID：`mengshouer.analysis-bilibili`

## 功能

- 自动解析视频：`BV` 号、`av` 号、视频链接，以及分 P 参数 `p` 和时间参数 `t`。
- 自动解析番剧：`ep`、`ss`、`md` 编号和对应链接。
- 自动解析直播间、专栏 `cv`、动态及 opus 链接。
- 跟随 `b23.tv`、`bili22.cn`、`bili23.cn`、`bili33.cn`、`bili2233.cn` 短链接。
- 解析 QQ 分享的 Bilibili 小程序卡片，包括嵌套 JSON 中的 `qqdocurl` 短链接。
- 从适配器保留的平台卡片元数据中提取 `qqdocurl`，精确解析原分享视频。
- 可选发送视频封面、番剧封面、直播封面、专栏图片和动态图片。
- 可选启用 `搜视频 <关键词>` 命令。
- 支持用户、群聊白名单和黑名单，以及按群隐藏视频或番剧简介。
- 支持按聊天流抑制同一内容的重复解析。

## 兼容性

- MaiBot：`1.2.0` 至 `1.2.99`
- MaiBot Plugin SDK：`2.7.1` 至 `2.99.99`
- Python 依赖：`aiohttp>=3.12.14,<4`

兼容范围以 `_manifest.json` 为准。

## 安装

在 MaiBot 根目录执行：

```bash
git clone https://github.com/493Arceus/maibot-bilibili-analyzer.git plugins/bilibili-analyzer
```

也可以下载仓库源码并手动解压到 MaiBot 的第三方插件目录。安装后的关键文件结构如下：

```text
MaiBot/
└── plugins/
    └── bilibili-analyzer/
        ├── assets/
        │   └── icon.png
        ├── _manifest.json
        ├── plugin.py
        ├── bilibili.py
        ├── requirements.txt
        ├── LICENSE
        └── README.md
```

必须放在实际运行实例的 `MaiBot/plugins/` 下。仅把插件保存在其他工作区目录中，不会被 MaiBot 自动发现。

插件 Runner 会根据 `_manifest.json` 检查并安装缺失依赖。若需要在独立环境中手动安装依赖，可执行：

```bash
python -m pip install -r requirements.txt
```

安装后启动 MaiBot，或等待插件文件 watcher 自动重载。正常加载时可以看到类似日志：

```text
插件 mengshouer.analysis-bilibili v1.0.5 加载成功
插件 mengshouer.analysis-bilibili 注册完成
Bilibili 解析插件已加载
```

然后在 WebUI 的插件管理页面确认插件处于“已启用”状态。

## 使用方法

### 自动解析

直接在群聊或私聊中发送支持的链接或编号，无需命令前缀。例如：

```text
BV1xxxxxxxxx
https://www.bilibili.com/video/BV1xxxxxxxxx
https://b23.tv/xxxxxxx
https://www.bilibili.com/bangumi/play/ep123456
https://live.bilibili.com/123456
cv123456
https://t.bilibili.com/123456789
```

QQ 中直接分享的 Bilibili 小程序卡片也会自动检查。即使 `analysis_use_on_message` 为 `false`，明确识别为 Bilibili 的小程序卡片仍会解析；该设置只控制其他结构化消息是否参与检查。

插件以观察模式处理消息，不会阻止 MaiBot 的其他消息处理流程。

### 搜索视频

先将 `analysis_enable_search` 设置为 `true`，然后发送：

```text
搜视频 关键词
```

插件会搜索 Bilibili，并解析搜索结果中的第一个视频。

只发送 `搜视频` 时会返回用法提示。搜索请求可能受到 Bilibili 风控限制；这不代表插件未加载。

### 图片显示

默认只发送文字。可通过以下两种方式开启图片：

- `analysis_display_image = true`：为所有支持的内容类型显示图片。
- `analysis_display_image_list = [...]`：只为列出的内容类型显示图片。

支持的类型值：

- `video`：视频封面
- `bangumi`：番剧封面
- `live`：直播封面
- `article`：专栏图片
- `dynamic`：动态或 opus 图片

若图片下载失败、图片超过 10 MiB 或图文消息发送失败，插件会尽量降级为纯文本结果。

## `config.toml` 配置说明

推荐通过 MaiBot WebUI 修改配置。不要把包含真实用户 ID、群 ID 或网络设置的运行配置提交到公开仓库。

除 `[plugin]` 外，下列 `analysis_*` 选项都直接位于 TOML 根级，不属于 `[general]` 配置节。`1.0.2` 已修复 WebUI 将这些顶层字段错误提交到 `general` 分区、导致布尔开关提示保存成功但值没有变化的问题；当前 `1.0.5` 会从平台卡片元数据读取 QQ 小程序的精确链接。

下面是使用脱敏示例值的完整配置示例：

```toml
[plugin]
enabled = true
config_version = "1.0.5"

analysis_images_size = ""
analysis_cover_images_size = ""
analysis_whitelist = []
analysis_group_whitelist = []
analysis_blacklist = []
analysis_group_blacklist = []
analysis_desc_blacklist = []
analysis_reanalysis_time = 0
analysis_display_image = false
analysis_display_image_list = []
analysis_enable_search = false
analysis_use_on_message = false
analysis_trust_env = false
analysis_request_timeout = 20.0
```

### `[plugin]`

#### `enabled`

- 类型：布尔值
- 默认值：`true`

控制插件是否启用。WebUI 的启停开关会修改该值。设置为 `false` 后，插件运行时会跳过激活插件组件。

#### `config_version`

- 类型：字符串
- 默认值：`"1.0.5"`

配置结构版本，由插件和运行时用于配置校验。除非插件升级说明明确要求，否则不要手动修改。

### 图片配置

#### `analysis_images_size`

- 类型：字符串
- 默认值：空字符串
- 示例：`"480w_90q"`

普通图片的 Bilibili CDN 缩放参数，适用于视频图片、专栏图片和动态图片。留空时使用原图地址。填写错误的参数可能导致图片下载失败。

#### `analysis_cover_images_size`

- 类型：字符串
- 默认值：空字符串
- 示例：`"480w_90q"`

封面类图片的 Bilibili CDN 缩放参数，主要用于番剧和直播封面。留空时使用原图地址。

#### `analysis_display_image`

- 类型：布尔值
- 默认值：`false`

是否为全部支持的内容类型显示图片。启用后，无需再逐项填写 `analysis_display_image_list`。

#### `analysis_display_image_list`

- 类型：字符串数组
- 默认值：`[]`
- 可选值：`video`、`bangumi`、`live`、`article`、`dynamic`

只为指定内容类型显示图片。例如：

```toml
analysis_display_image = false
analysis_display_image_list = ["video", "bangumi"]
```

当 `analysis_display_image` 为 `true` 时，该列表不再限制图片类型。

### 权限配置

所有 ID 都按字符串比较，建议在 TOML 中使用带引号的字符串。

#### `analysis_whitelist`

- 类型：字符串数组
- 默认值：`[]`

允许使用插件的用户 ID 白名单。

#### `analysis_group_whitelist`

- 类型：字符串数组
- 默认值：`[]`

允许使用插件的群 ID 白名单。

白名单规则：只要用户白名单或群白名单中任意一个非空，插件就进入白名单模式。消息发送者命中用户白名单，或者消息所在群命中群白名单，才允许解析。白名单模式下不再检查黑名单。

示例：

```toml
analysis_whitelist = ["10000001"]
analysis_group_whitelist = ["20000001", "20000002"]
```

#### `analysis_blacklist`

- 类型：字符串数组
- 默认值：`[]`

禁止使用插件的用户 ID 黑名单。仅在两个白名单都为空时生效。

#### `analysis_group_blacklist`

- 类型：字符串数组
- 默认值：`[]`

禁止使用插件的群 ID 黑名单。仅在两个白名单都为空时生效。

示例：

```toml
analysis_whitelist = []
analysis_group_whitelist = []
analysis_blacklist = ["10000002"]
analysis_group_blacklist = ["20000003"]
```

#### `analysis_desc_blacklist`

- 类型：字符串数组
- 默认值：`[]`

不显示简介的群 ID 列表。目前用于隐藏视频和番剧解析结果中的简介，不影响标题、作者、统计数据和链接。

### 解析行为

#### `analysis_reanalysis_time`

- 类型：非负整数，单位为秒
- 默认值：`0`

控制同一聊天流中重复内容的抑制：

- 大于 `0`：同一个规范化链接在指定秒数内只解析一次。
- 等于 `0`：只记住上一条已解析链接；连续发送同一链接时抑制，期间解析其他链接后可以再次解析原链接。

缓存按聊天流隔离，群聊和私聊之间不会互相影响。

#### `analysis_enable_search`

- 类型：布尔值
- 默认值：`false`

是否启用用户主动调用的 `搜视频 <关键词>` 命令。只有该主动命令会按标题搜索并解析首个结果；QQ 小程序自动解析不会使用标题猜测。

#### `analysis_use_on_message`

- 类型：布尔值
- 默认值：`false`

是否检查文本段之外的全部结构化消息内容。为了保证 QQ 分享可用，明确识别为 Bilibili 的小程序卡片始终检查，不受该开关影响。

通常保持 `false` 即可；只有适配器把需要解析的内容放在其他结构化消息段中时才建议启用。

### 网络配置

#### `analysis_trust_env`

- 类型：布尔值
- 默认值：`false`

是否让 `aiohttp` 继承系统代理相关环境变量，例如 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NO_PROXY`。

默认关闭可以避免意外使用系统代理。只有部署环境明确依赖代理访问 Bilibili 时才应开启。

#### `analysis_request_timeout`

- 类型：浮点数，单位为秒
- 默认值：`20.0`
- 允许范围：`3.0` 至 `60.0`

Bilibili API、短链跳转和图片请求使用的总超时时间。连接超时最多为 8 秒，并且不会超过该值。网络较慢时可以适当增大，但过大会延长 Hook 等待时间。

## 其他文件说明

### `_manifest.json`

这是 MaiBot 插件运行时的清单文件，通常不需要用户修改。

- `manifest_version`：清单格式版本，当前为 `2`。
- `id`：插件唯一 ID，当前为 `mengshouer.analysis-bilibili`。
- `version`：插件版本。
- `name`、`description`、`author`、`license`：展示和归属信息。
- `urls`：仓库、主页、文档和问题反馈地址。
- `host_application`：兼容的 MaiBot 版本范围。
- `sdk`：兼容的 MaiBot Plugin SDK 版本范围。
- `dependencies`：Runner 检查和安装的 Python 依赖。运行时以这里的约束为准。
- `capabilities`：插件申请的宿主能力；当前为 `send.text` 和 `send.hybrid`。
- `i18n`：本地化资源声明。
- `plugin_type`：插件类型，当前为第三方扩展 `extension`。
- `display`：WebUI 图标与颜色配置。

随意修改 `id`、兼容范围、依赖或能力声明，可能导致插件无法加载、被识别成另一个插件，或无法发送消息。

### `requirements.txt`

用于独立环境手动安装依赖：

```text
aiohttp>=3.12.14,<4
```

MaiBot Runner 安装插件依赖时以 `_manifest.json` 的 `dependencies` 为准；修改依赖版本时应同步更新这两个文件。

### `plugin.py`

插件入口、配置模型、消息 Hook、搜索命令、权限判断、去重和消息发送逻辑。工厂入口为 `create_plugin()`。

### `bilibili.py`

链接和编号识别、短链跳转、Bilibili API 请求、WBI 签名、内容格式化和图片下载逻辑。

## 常见问题

### WebUI 能看到插件，但启用后仍显示未激活

确认 `[plugin]` 中存在：

```toml
enabled = true
```

然后查看日志中是否出现“已在配置中禁用，跳过激活”或启停同步超时。不要连续反复点击开关；先确认一次操作的运行时结果。

### 插件目录存在，但 WebUI 列表中没有

确认：

1. 插件位于 MaiBot 当前工作目录下的 `plugins/bilibili-analyzer/`。
2. `_manifest.json` 和 `plugin.py` 都存在。
3. `_manifest.json` 是合法 JSON，且 MaiBot/SDK 版本处于声明的兼容范围内。
4. 插件目录不是指向插件根目录外部的符号链接。
5. 重启后刷新 WebUI 插件页面，避免使用后端重启前的页面状态。

### 普通链接可以解析，但 QQ 小程序不能解析

确认适配器保留了 QQ JSON 卡片的平台元数据。`1.0.5` 会读取 `message_info.additional_config.platform_card_payloads`，并从小程序原始 payload 的 `qqdocurl` 提取精确短链；兼容旧链路直接放在结构化消息段中的 `qqdocurl`。

如果消息只剩 `[小程序] 哔哩哔哩：标题`，同时没有 `qqdocurl`、BV/AV 号或明确链接，插件不会按标题自动搜索。标题不能唯一定位视频，宁可不解析也不会发送可能错误的同名视频。

`analysis_use_on_message` 可以保持为 `false`，因为明确识别的 Bilibili 小程序卡片元数据始终检查。

### 搜索提示风控或需要验证

Bilibili 搜索接口可能返回 `-412` 或额外验证要求。插件会给出风控提示。可以稍后重试，检查出口网络，或确认代理配置是否符合预期；不要通过降低安全边界或伪造结果来绕过验证。

### 图片没有显示

检查：

- `analysis_display_image` 或 `analysis_display_image_list` 是否正确配置。
- 图片缩放参数是否有效。
- 部署网络能否访问 Bilibili 图片域名。
- 图片是否超过 10 MiB。
- 日志中是否出现图片下载失败或图文发送失败信息。

即使图片失败，插件通常仍会发送纯文本解析结果。

`1.0.4` 修复了大封面采用 HTTP 分块传输时，插件只读取并发送首个网络分块的问题。该问题通常表现为图片顶部正常、下方大面积灰色；升级后插件会持续读取到响应结束，同时继续执行 10 MiB 大小限制。

### WebUI 开关提示已保存，但刷新后恢复原值

请确认插件版本不低于 `1.0.2`。旧版本会把根级的 `analysis_display_image`、`analysis_enable_search` 等字段错误映射到 `general` 展示分区，Runner 校验时会忽略该无效分区。`1.0.2` 将展示分区的数据路径显式映射回配置根级，保存后应立即触发热更新。当前版本为 `1.0.5`。

### 修改配置后多久生效

插件支持配置热更新，但 watcher 只监听实际部署目录中的 `config.toml`。例如远端插件位于 `/home/superuser/maimai/MaiBot/plugins/bilibili-analyzer/` 时，必须修改该目录下的配置；修改工作区中未部署的副本不会影响运行实例。

除 `plugin.enabled` 外，普通配置字段变化会触发 `on_config_update`：插件使用宿主已经校验并注入的新配置重建 HTTP 客户端，同时清空重复解析缓存，无需重启整个 MaiBot。成功时日志类似：

```text
Bilibili 解析配置已热更新: config_version=1.0.5，HTTP 客户端已重建，重复解析缓存已清空
```

`plugin.enabled` 是宿主管理的特殊字段：禁用时卸载插件，重新启用时再次加载插件，因此不走普通配置更新回调。

## 安全说明

- 不要在仓库中提交真实用户 ID、群 ID、代理凭据或其他运行环境敏感信息。
- 默认 `analysis_trust_env = false`；只有明确需要系统代理时才开启。
- 短链接仅跟随插件支持的 Bilibili 短链域名。
- 图片下载限制为 10 MiB。
- 插件清单中只申请文本和图文发送能力，不申请数据库或文件写入能力。

## 维护、致谢与许可证

当前维护者：[493Arceus](https://github.com/493Arceus)

问题反馈：<https://github.com/493Arceus/maibot-bilibili-analyzer/issues>

本项目迁移并改写自 mengshouer 的 NoneBot 插件：
<https://github.com/mengshouer/nonebot_plugin_analysis_bilibili>

本插件按 `_manifest.json` 声明使用 [MIT License](LICENSE)。
