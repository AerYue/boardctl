# boardctl — 开发板控制 MCP Server

> **开发说明**：本项目由GLM-5.3-Flash开发。

boardctl 是一个 MCP server，让 AI 编程助手（ZCode、Claude、Cursor 等任何支持 MCP 的工具）
直接操控 Linux 开发板的控制台：**串口（UART/USB 转串口）、SSH、Telnet 三种通道，一套会话式接口**。
人可以用自己习惯的终端软件（WindTerm / Xshell / MobaXterm / PuTTY）接入同一个控制台，人机协同调试。

```
 AI 编程工具（ZCode 等）        人（WindTerm / Xshell / MobaXterm）
        │ MCP (stdio)                   │ Telnet 127.0.0.1:port
        ▼                               ▼
 ┌───────────────────── boardctl ─────────────────────┐
 │  会话管理 · expect 等待 · 环形缓冲 · 共享桥         │
 └──────┬──────────────┬──────────────┬───────────────┘
      串口 COMx       SSH 22        Telnet 23
        └───────────────┴──────────────┘
                        ▼
                  Linux 开发板
```

与"一次执行一条命令"式 SSH 工具的本质区别：boardctl 保持**有状态交互会话**，
能等待登录提示、U-Boot 菜单、y/n 确认、命令完成标记——这才是驱动开发板控制台的关键。

> 给 AI 助手：操作请读 [AI_GUIDE.md](AI_GUIDE.md)。

## 功能总览

| 工具 | 用途 |
|---|---|
| `serial_list()` | 列出本机 COM 口（含描述/硬件 ID，方便认出板子的 USB 串口） |
| `connect(type, ...)` | 建立会话，返回 `session_id`；serial / ssh / telnet 三选一 |
| `send(session_id, data)` | 发送一行命令并收集回复（支持发原始字节，如 Ctrl+C） |
| `read(session_id, timeout)` | 读取新输出（开机日志、异步消息） |
| `expect(session_id, patterns)` | **等待正则出现**（`login:`、`~#`、`Hit any key`...），控制台自动化核心 |
| `control_lines(session_id, dtr, rts)` | 拉拉 DTR/RTS 控制线（板子复位、进 bootloader），仅串口会话 |
| `share(session_id, port)` / `unshare(session_id)` | **人机共享**：把会话暴露成本地 Telnet 口，终端软件接入同一控制台 |
| `sessions()` / `close(session_id)` | 会话管理（`sessions()` 标出已共享端口） |
| `ssh_exec(host, username, command)` | 一次性 SSH 执行（免会话，回 stdout/stderr/exit_code） |
| `sftp_upload` / `sftp_download` | 一次性 SFTP 传文件（推固件 / 拉日志） |

另有三个命令行脚本（不需要 AI、手工也能用，位于 `src/`）：

| 脚本 | 用途 |
|---|---|
| `share_console.py` | 独立起共享控制台：`python src/share_console.py COM6`，Ctrl+C 退出并释放板子 |
| `board_probe.py` | 串口探测：识别控制台状态（登录提示/shell）、试常见口令 |
| `board_probe_ssh.py` | SSH 冒烟：试口令 → 交互命令 → SFTP 回环 |

## 环境要求

- Windows / Linux / macOS 均可（开发验证环境为 Windows 10/11）
- Python **3.10 及以上**
- 板子侧无任何要求：串口有控制台即可；SSH 需板子开了 sshd（老 Dropbear 也行）；
  Telnet 需板子开了 telnetd
- 共享桥只绑定 127.0.0.1，不暴露局域网，无防火墙配置

## 安装

**第 1 步：获取代码**

```
git clone https://github.com/AerYue/boardctl.git
# 或下载 zip 解压
```

**第 2 步：装依赖**

```
python -m pip install -r requirements.txt
```

依赖只有三个：`mcp>=1.10,<2`（MCP 协议）、`pyserial>=3.5`（串口）、`paramiko>=3.5`（SSH/SFTP）。
注：paramiko ≥5 已移除 ssh-rsa 老算法，boardctl 内置 legacy 垫片自动兼容老板子（见"典型场景"）。

**第 3 步：注册到 AI 工具**

ZCode 用户级配置 `~/.zcode/cli/config.json`，在 MCP servers 节点加入（路径换成实际安装路径）：

```json
"boardctl": {
  "type": "stdio",
  "command": "C:\\Python314\\python.exe",
  "args": ["C:\\path\\to\\boardctl\\src\\boardctl_mcp.py"]
}
```

其他 MCP 客户端（Claude Desktop 等）等价的通用写法：

```json
{
  "mcpServers": {
    "boardctl": {
      "command": "python",
      "args": ["C:\\path\\to\\boardctl\\src\\boardctl_mcp.py"]
    }
  }
}
```

重启 AI 工具后生效，工具名前缀为 `boardctl__*`。

**第 4 步：验证**

```
python tests/test_boardctl.py        # 67/67 checks passed（无需真实硬件）
```

或直接让 AI 调一次 `serial_list()`，能列出 COM 口即为连通。

## 快速上手

**串口控制台**（新板子首次上电，让 AI 执行的典型序列）：

```
serial_list()                                  # 找到板子的 COM 口
connect(type="serial", serial_port="COM6")     # 115200-8N1，返回登录提示
expect(["login:"])                             # 等登录提示
send("root") → expect(["Password:"]) → send("口令")
expect(["~#"])                                 # 拿到 shell 提示符
send("uname -a && dmesg | tail")
close(session_id)
```

**SSH**（免交互执行 + 交互会话）：

```
ssh_exec(host="192.168.5.10", username="root", password="...", command="ps | grep app")
connect(type="ssh", host="192.168.5.10", username="root", password="...", keepalive=30)
sftp_upload(host=..., local_path="app.bin", remote_path="/tmp/app.bin")
```

**人机共享控制台**：

```
（AI 侧）connect(...) → share(session_id)      # 返回 listen_port，如 3634
（人侧）WindTerm/Xshell/MobaXterm 新建 Telnet 会话：127.0.0.1:3634
```

两边看到、操作的是同一个控制台：板子输出双向镜像，谁敲的命令对方都可见；
最多 4 个终端同时接入；新接入者先收到最近 ~4KB 历史；回显由板子负责，终端不会双重显示。

## 典型场景

**首次连一块陌生板子**：让 AI 走探测流程——按回车看提示符 → 是登录就登 → 顺手跑
`uname -a` / `ip addr` 摸底。配套脚本 `python src/board_probe.py COM6 115200` 可脱离 AI 手工探测。

**刷写 / 长编译监控**：`send(..., wait=0)` 立即返回，再 `expect(["完成标记", "ERROR"])` 轮询；
SSH 会话加 `keepalive=30` 防空闲断线。

**老板子 SSH 兼容（ssh-rsa / Dropbear 2014–2017）**：paramiko ≥5 已删除 ssh-rsa，
而那个年代的 Yocto 镜像只提供它。boardctl 遇到 "no acceptable host key" 会**自动**带
legacy 垫片重试一次，成功后结果标记 `"legacy_algos": true`；也可显式传 `legacy_algos=true`。
垫片按连接实例隔离，不影响新板子的安全默认值。

**板上没有 SFTP 时传文件**：精简镜像常缺 openssh-sftp-server，SFTP 会报
"EOF during negotiation"（错误信息自带提示）。替代方案：用 `ssh_exec` 走 base64 分块过控制台，
配方见 `AI_GUIDE.md`。

**进 bootloader / 复位**：`control_lines(session_id, dtr=False, rts=True)` 再拉回来
（ESP32/STM32 类芯片的 ROM 下载模式）。注意：对会复位的板子，这是真复位，别误用。

## 内置限制（默认值）

| 项 | 值 |
|---|---|
| 并发会话数 | 最多 16 |
| 会话缓冲 | 1MB 环形，超限裁剪到 600KB（最早的内容丢弃） |
| 单次返回截断 | send 8000 字符 / read 16000 / expect 4000 / connect 8000（保留尾部，`truncated`+`dropped_chars` 标记） |
| ssh_exec | 每流 32KB；整条命令受 timeout 墙钟限制（超时返回已收到的部分输出） |
| 共享桥客户端 | 每会话 4 个，仅 127.0.0.1 |
| 串口默认参数 | 115200-8N1，无流控 |
| 编码 | 默认 utf-8（可按会话指定，如 `encoding="gb18030"`） |

## 故障排查 FAQ

| 现象 | 原因与处置 |
|---|---|
| `could not open port 'COMxx'` | 口号不存在/拼错，用 `serial_list()` 核对（CH340/FTDI/CP210x 的 USB 串口通常描述可见） |
| 串口打开报拒绝访问 | **COM 口独占**：被别的进程占着——之前未关的会话、share_console.py、其他终端软件。关掉即可 |
| `no acceptable host key` | 老板子只提供 ssh-rsa。已自动重试兼容；若仍失败，板子连 ssh-rsa 都没有，需升级板子 sshd |
| 更老板子 SSH 彻底连不上 | 只有 SHA-1 密钥交换算法（2013 年前的 Dropbear/OpenSSH 5.x），paramiko ≥5 已无此实现也无法垫片——走串口控制台，或给板子升级 sshd |
| `Authentication failed` | 口令不对。可用串口控制台 `echo root:新口令 \| chpasswd` 重设 |
| SFTP 报 `EOF during negotiation` | 板子没装 sftp-server（dropbear 需另装 openssh-sftp-server）；走 base64 过控制台 |
| 命令里中文变乱码 | 板子 busybox shell 是 C locale、按字节处理。控制台命令用 ASCII |
| 按键回显慢、不跟手 | 老版本串口读线程缺陷，已修复（`read(in_waiting or 1)`）。确认分发的是新版 |
| SSH 长任务中途断 | 空闲被掐。`connect(..., keepalive=30)` |
| telnet 连不上 | 板子 telnetd 没开 / IP 不通，先 `ping` |
| 想看后台发生了什么 | 看 `src/boardctl.log`（运行时自动生成于 src/ 下，轮转保留 3 份） |

## 测试与维护

```
python tests/test_boardctl.py        # 端到端 67 项检查，无需真实硬件
                                     # （假 telnetd + 假 sshd + ssh-rsa-only 老 sshd + 串口回环）
python src/board_probe.py COM6 115200          # 真机串口探测
python src/board_probe_ssh.py 192.168.5.10     # 真机 SSH 冒烟（自动试常见口令）
```

改代码后跑一遍 `python tests/test_boardctl.py` 即可回归；真机冒烟脚本为可选的上线前验证。

## 目录结构

```
boardctl/
├── README.md            # 本文件（用户手册）
├── AI_GUIDE.md          # 给 AI 看的操作指南
├── LICENSE              # MIT
├── requirements.txt     # 依赖清单
├── .gitignore
├── .gitattributes       # 换行符与二进制属性
├── src/
│   ├── boardctl_mcp.py      # server 主体（唯一必需的代码文件）
│   ├── share_console.py     # 独立共享控制台
│   ├── board_probe.py       # 串口探测脚本
│   └── board_probe_ssh.py   # SSH 冒烟脚本
└── tests/
    └── test_boardctl.py     # 端到端测试（67 项，无需硬件）
```

## 许可证

[MIT](LICENSE) — 欢迎自由使用、修改与分发；issue 和 PR 均欢迎。
