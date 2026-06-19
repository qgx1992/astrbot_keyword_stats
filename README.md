# astrbot_keyword_stats

<div align="center">

**AstrBot 插件：关键词自动回复 + 群聊消息统计**

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-blue)
![Version](https://img.shields.io/badge/version-1.1.5-green)
![License](https://img.shields.io/badge/license-MIT-orange)

</div>

---

## 功能

- 关键词自动回复
- 群统计
- 今日统计
- 昨日统计
- 月统计
- 导出活跃榜
- 今日发言（查看自己）
- 清空统计（仅群主/管理员）
- 关键词列表
- 添加关键词（仅群主/管理员）
- 统计榜优先显示群昵称/群名片，取不到时回退为 QQ 号
- 发言榜显示格式为：`群昵称（QQ号）`
- 支持查询单个成员发言统计：`查发言 @某人` / `查发言 QQ号` / `查发言 群昵称`

## 可用指令

普通成员可用：

- `群统计`
- `今日统计`
- `昨日统计`
- `本周统计`
- `月统计`
- `今日发言`
- `导出活跃榜`
- `关键词列表`
- `查发言 @某人`
- `查发言 QQ号`
- `查发言 群昵称`

群主/管理员可用：

- `清空统计`
- `添加关键词 关键词=回复内容`
- `删除关键词 关键词`

说明：
- 不需要命令前缀，直接发中文即可。
- 也兼容带 `/` 的写法，例如 `/群统计`。

## 默认关键词

关键词已独立存储，不再通过插件配置页直接编辑。

默认关键词为：

- `你好`
- `在吗`
- `帮助`

关键词会保存在插件数据目录中的 `keywords.json`，可通过群内命令维护：
- `关键词列表`
- `添加关键词 关键词=回复内容`
- `删除关键词 关键词`

## 插件配置

本插件已接入 AstrBot 标准配置 schema。安装后可在插件页面直接看到配置项，不再显示"没有配置"。

当前可配置项：
- `group_whitelist`：允许插件生效的群号列表
- `match_mode`：匹配模式（contains / exact）
- `reply_once_per_message`：单条消息是否只回复一次

注意：
- 关键词不再放在插件配置页中，避免整份覆盖导致误删
- 关键词通过群内命令单独维护，并持久化保存在 `keywords.json`
- 修改配置后建议重载插件或重启 AstrBot

可在 `config.json` 中配置：

```json
"group_whitelist": [123456789, 987654321]
```

说明：
- 留空 `[]`：表示所有群都允许使用本插件功能
- 填入群号后：只有白名单里的群会参与统计，并且只有这些群能使用本插件的所有功能（包括关键词回复、统计命令、关键词管理）
- 非白名单群发送相关命令时，会提示：`当前群未加入统计白名单`

运行后会在 `data/stats_data.json` 中保存群消息统计数据。

## 安装

### 通过 AstrBot 插件市场

在 AstrBot 插件页面搜索 `astrbot_keyword_stats` 并进行安装。

### 通过 GitHub 安装

```bash
# 克隆仓库
git clone https://github.com/<你的用户名>/astrbot_keyword_stats.git

# 将插件目录复制到 AstrBot 的插件目录
cp -r astrbot_keyword_stats <AstrBot目录>/addons/
```

AstrBot 上传安装时，请使用 **flat** 结构 zip（zip 根目录直接包含插件文件）。
如果无法覆盖安装，请先卸载旧版本，再安装新版本。

## 数据文件

插件运行时会在 `data/` 目录下生成以下文件：

| 文件 | 说明 |
|------|------|
| `stats_data.json` | 群消息统计数据（自动生成，勿手动编辑） |
| `keywords.json` | 关键词回复数据（通过群内命令管理） |

这些数据文件已在 `.gitignore` 中排除，不会上传到 GitHub。

## 开发

```bash
# 克隆项目
git clone https://github.com/<你的用户名>/astrbot_keyword_stats.git
cd astrbot_keyword_stats

# 代码结构
# ├── __init__.py          # 插件入口
# ├── main.py              # 插件主逻辑
# ├── metadata.yaml        # 插件元数据
# ├── config.json          # 默认配置
# ├── _conf_schema.json    # 配置 Schema
# └── data/                # 运行时数据目录
```

## License

MIT
