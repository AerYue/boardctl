# boardctl AI 操作指南

本文件写给通过 MCP 调用 boardctl 的 AI 助手。工具的权威签名以 `list_tools` 返回的
docstring 为准；本文件讲**怎么用好它们**——心智模型、决策规则、实战技巧和踩过的坑。

## 1. 心智模型（先读这个）

- **会话是有状态的**。`connect` 返回 `session_id`，之后所有调用都靠它。会话跨工具调用存活，
  用完 `close`，最多同时 16 个。
- **输出是字节流，不是命令应答**。板子的回显、提示符、人类敲的键（共享时）全部混在同一条流里。
  忘掉"请求-响应"直觉，用"流 + 等待标记"思考。
- **两条独立游标**：`read()`/`send()` 消费读游标；`expect()` 有自己的扫描游标。
  先读掉的内容不影响 expect 扫描，反之亦然——所以开机刷屏读掉了也能再等标记。
- **所有工具永不抛异常**，失败一律 `{"ok": false, "error": "..."}`，先判 `ok`。
- **输出保尾部**：超过 `max_output` 只留尾部并带 `truncated: true` / `dropped_chars`。
- **缓冲 1MB 环形**：太久没消费的输出会被裁掉最早的（日志有 WARN）。

## 2. 工具选择决策

| 场景 | 用什么 |
|---|---|
| 跑一条不需要交互的命令（有 sshd） | `ssh_exec`（免会话、有 exit_code，最快） |
| 要看提示符/确认/交互流程，或走串口 | `connect` + `send`/`expect` |
| 等某件事发生（登录提示、编译完成、错误出现） | `expect`（控制台自动化核心） |
| 收异步输出（开机日志、内核消息） | `read` |
| 传文件 | `sftp_upload/download`；板子没 sftp-server 时走 §7 配方 |
| 人要盯着/一起操作 | `share`，把 `listen_port` 告诉用户 |
| 复位板子/进 bootloader | `control_lines`（谨慎，见 §8） |

## 3. 标准流程

**串口首连（可能要登录）**：

```
serial_list() → connect(type="serial", serial_port="COM6")
# 先按回车探状态，别急着输用户名：
send("\n", newline="none", wait=1.5)
# 尾部是 shell 提示符 → 已登录；是 "login:" → 登录流程：
send("root") → expect(["Password:", "login:"], timeout=5) → send("口令") → expect([r"~# \$"])
```

**发命令的两种姿势**：

- 快命令：`send("uname -a", wait=2)`，回显+输出+提示符一次拿全。
- 慢命令（刷写/编译/重启）：`send(cmd, wait=0)` 立即返回，然后 `expect(["DONE-0", "ERROR"], timeout=120)` 轮询，
  超时了继续 expect 或 `read` 看进展。

**收尾**：`close(session_id)`——尤其串口，不关会占着 COM 口（Windows 串口独占，
之后谁也打不开，包括 share_console.py）。

## 4. expect 模式技巧（真机踩坑实录）

- **标记要和回显不同**。教训：发 `echo "IP-QUERY-DONE"` 后等 `IP-QUERY-DONE`，
  结果匹配到的是**命令回显本身**（回显里就含这个字符串），真正的输出还没到。
  正确姿势：**标记里带 `$?`**——
  ```
  send('dmesg | tail; echo "DONE-$?"', wait=0)
  expect(["DONE-0", "DONE-"])     # 回显里是字面 DONE-$?，输出里才是 DONE-0
  ```
  回显显示 `DONE-$?`（未展开），输出显示 `DONE-0`（已展开），天然区分。
- 提示符正则按板子写具体值：`[r"root@ALIENTEK-IMX6U:~#"]` 比 `"#"` 安全得多，
  不会匹配到注释行或文件内容。
- 多模式并列是"或"关系，返回 `pattern_index` 告诉你中哪个，用它分支：
  `expect(["Password:", "Login incorrect"])` → index 0 继续输密码，index 1 重来。
- timeout 到了返回 `matched: false` + 当前缓冲尾部——**尾部本身就是线索**
  （板子在等输入？输出停了？会话死了？看尾部判断）。
- 匹配后输出从上次匹配位置起算——别重复匹配同一标记，游标已越过它。

## 5. 人机共享礼仪（share）

```
share(session_id) → 告诉用户："WindTerm/Xshell 连 Telnet 127.0.0.1:<listen_port>"
```

- 你的每次 `send` 用户都看得见，用户敲的每个键也会进你的流（含退格、Ctrl+C）——
  流里出现陌生片段先想到"人在打字"。
- **用户打字时别发命令**，等流安静（`read` 返回尾部是稳定提示符）再动。
- 自己的长命令一律带唯一标记（§4），避免和人的输入互相污染判断。
- 结束共享：`unshare`；关会话会自动拆桥。

## 6. 文件传输

- 首选 `sftp_upload` / `sftp_download`（需板子有 sftp-server）。
- 报 `EOF during negotiation` = 板子没 sftp-server，改走 **base64 过控制台**：

```
# 推文件（PC → 板子），分块 ~1KB/行，单引号防展开：
#   本地: certutil -encode app.bin app.b64   (Windows) 或 base64 app.bin > app.b64
#   板上: 先 rm /tmp/app.b64
send("echo 'AAAA' >> /tmp/app.b64")     # 每块一条，逐块发
send("base64 -d /tmp/app.b64 > /tmp/app")
ssh_exec(command="md5sum /tmp/app")      # 与本地 md5 比对
# 拉文件（板子 → PC）：base64 /tmp/log.txt 分块读回，本地拼接后解码
```

- 大文件（>几百 KB）过控制台很慢，优先想办法装 sftp-server 或用 U 盘/SD 卡。

## 7. SSH 专项

- 免交互一条命令：`ssh_exec`（有 `exit_code`；stdout/stderr 各 32KB 上限）。
- 交互会话：`connect(type="ssh", ..., keepalive=30)`——长任务必带 keepalive。
- 老板子（Dropbear 2014–2017）：遇 `no acceptable host key` 会**自动**带 ssh-rsa 垫片重试，
  成功后返回带 `"legacy_algos": true`，照常用即可，无需特殊处理。
- 交互会话里判断命令成功：`echo "RC-$?"` + `expect(["RC-0"])`（同 §4 技巧）。

## 8. 安全红线

- **`control_lines` 是真复位**：dtr/rts 一拉，会复位的板子立刻重启、未保存状态全丢。
  只在明确要做 bootloader/复位流程时用，且先跟用户确认。
- 串口 COM 口独占：connect 报拒绝访问时，查 `sessions()` 里有没有活会话、
  用户的终端软件、share_console.py 是否占着口，而不是反复重试。
- 口令类信息出现在 `send` 的 data 里属正常（登录必需），但不要把口令写进
  expect 的 patterns 或日志输出。
- 会话别囤积：`MAX_SESSIONS=16`，用完即 close。

## 9. 常见错误速查

| 错误串 | 含义 | 处置 |
|---|---|---|
| `could not open port 'COMxx'` | 口不存在 | `serial_list()` 核对 |
| 打开串口拒绝访问 | 口被占用 | 查活会话/终端软件，先 close |
| `no acceptable host key` | 老板子只有 ssh-rsa | 已自动兼容重试；仍失败=板子 sshd 太烂，走串口 |
| `Authentication failed` | 口令错 | 串口 `chpasswd` 重设，或问用户 |
| `EOF during negotiation`（sftp） | 无 sftp-server | §6 base64 配方 |
| `unknown session_id` | 会话已关/记错 | `sessions()` 重新发现 |
| 输出全是乱码 | 波特率不对 | 换 9600/57600/230400 重连 |
| 中文命令变乱码/铃响 | 板子 C locale | 控制台只用 ASCII |
| `timeout` 且 alive=false | 板子/连接死了 | `read` 看尾部，重连 |

## 10. 最小速查卡

```
serial_list()                                     # 找口
connect(type="serial", serial_port="COM6")        # 或 type="ssh/telnet"
send("\n", newline="none")                        # 探状态
send("root") → expect(["Password:"])              # 登录
send(cmd, wait=0) → expect(["MARK-0"])            # 慢命令
send("\x03", newline="none")                      # Ctrl+C
share(session_id)                                 # 给人开窗口
sessions()                                        # 找回 sid
close(session_id)                                 # 释放
```

完整用户文档见同目录 `README.md`；运行日志 `boardctl.log`（给人排查用）。
