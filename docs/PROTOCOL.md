# 华为 lite wearable 音乐协议摘要

本文记录本项目实现所依据的协议细节，全部来自对 [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)（AGPL-3.0）和 [HuaSideload](https://github.com/Burak4Arslan/huawei-watch-sideload-ios)（AGPL-3.0）源码的阅读与整理。目标设备是华为 GT 系列（lite wearable），不是 HarmonyOS 旗舰的完整协议。

## 1. GATT

| 用途 | UUID |
| --- | --- |
| 服务 | `0000fe86-0000-1000-8000-00805f9b34fb` |
| 写入（手机 → 手表） | `0000fe01-0000-1000-8000-00805f9b34fb` |
| 通知（手表 → 手机） | `0000fe02-0000-1000-8000-00805f9b34fb` |

写入使用 Write With Response（本项目默认），也可用 Write Without Response 换取速度。每次写入不超过协商的 ATT MTU − 3；GT 手表通告的分片大小通常是 `0xF4`（244），对应 MTU 247。

## 2. 帧格式

```
+------+-----------+--------+--------+----------+---------+-----------+--------+
| 0x5A | len (BE16)| piece  | seq    | service  | command | body ...  | CRC16  |
+------+-----------+--------+--------+----------+---------+-----------+--------+
```

- `len` = 2 + body 长度 + 1（包含 `piece`/`seq`，不含 CRC）。单帧时 `piece = 0x00`。
- 分片时 `piece` 依次为 `0x01`（首片）、`0x02`（中间片）、`0x03`（末片）；`seq` 从 0 开始递增。
- 首片携带 `service`/`command`，后续分片直接跟数据。
- CRC 为 **CRC-16/XMODEM**（poly `0x1021`，init `0x0000`，不反射，无 xorout），覆盖除末尾 CRC 外的整帧，大端存放。
  - 校验值：`crc16(b"123456789") == 0x31C3`。
- 示例（LinkParams 请求）：

```
5A 00 0B 00 01 01 01 00 02 00 03 00 04 00 F1 3B
```

## 3. TLV 与 VarInt

每个 TLV 项：

```
tag (1 字节) | varint 长度 | value
```

VarInt 为大端、每字节 7 位，最高位 `0x80` 表示后续还有字节。例如 `300 = 0x82 0x2C`。

常见 tag：

| tag | 含义 |
| --- | --- |
| `0x7F` | 结果码，成功为 `0x000186A0`（100000） |
| `0x7C` / `0x7D` / `0x7E` | 加密标志 / IV / 密文 |
| `0x82` / `0x83` | 容器 / 子项（歌曲列表等） |

少数命令的 body 不是 TLV 而是裸数据：`(0x28, 0x06)` 文件块、`(0x0A, 0x05)`、`(0x2C, 0x05)`、`(0x1C, 0x05)`。

## 4. 配对（HiChain）

顺序固定为：

1. `0x01/0x01` LinkParams（不加密）
   - 请求：`0x01 0x02 0x03 0x04`（空值）
   - 响应：`0x01` 协议版本、`0x02` 分片大小、`0x03` MTU、`0x05` `[0x00, authVersion, serverNonce(16)]`、`0x07` 设备类型、`0x08` 认证算法、`0x09` 绑定状态、`0x0C` 加密方式（1 = GCM，0 = CBC）。
2. `0x01/0x33` SecurityNegotiation（不加密）
   - 请求：`0x01` 认证模式（设备类型 4 用 `0x04`，否则 `0x02`）、`0x05` 本机身份（16 随机字节的大写 hex ASCII，32 字节）、`0x03 0x01`、`0x04 0x00`；`0x04` 模式再加 `0x06`、`0x07` 手机型号；GCM 时加 `0x0D 0x01`。
   - 响应：`0x01`/`0x02`/`0x7F` 给出认证类型，`0x05` 可能是 64 字节 UDID。
   - `type == 100000`，或 `type ^ 1 == 4`，或 `type ^ 2 == 4` → HiChain（下面的第 3–4 步）。
   - `type == 0x01` 或 `0x02`（设备类型 1–3，例如 GT 2e）→ HiChain Lite（见 4b）。
3. `0x01/0x2C` PinCode（不加密）
   - 请求：`0x01` 空值。
   - 响应：`0x01` 密文、`0x02` IV。密钥是固定 digest secret（按 authVersion 选 V1/V2/V3）；GCM 时用 `AES-GCM` 直接解密，否则 `AES-CBC` + PKCS#7。
   - 解出的 PIN（通常是 6 位数字的 ASCII）做 `key = SHA256(PIN.hex().upper())`。
4. `0x01/0x28` HiChain 四步（不加密，可能分片）

   HiChain 的 JSON 放在 TLV `0x01`，`0x02` 是操作码（1 = 用 PIN 认证，2 = 用保存的 token 绑定），`0x03` 是 8 字节 requestId。

   - **第 1 步（手机 → 手表）**：`payload` 含 `isoSalt`（本机随机 16 字节）、`peerAuthId`、`operationCode`、`seed`（随机 32 字节）、`peerUserType`；op2 另加 `pkgName`、`serviceType`、`keyLength`。
   - **第 1 步响应（手表 → 手机）**：`isoSalt`（手表随机 16 字节）、`peerAuthId`、`token`。
     - `psk = HMAC-SHA256(key, seed)`
     - 手表 token = `HMAC-SHA256(psk, rand_peer || rand_self || selfAuthId || peerAuthId)`
     - `isoKey = HKDF-SHA256(psk, salt = rand_self || rand_peer, info = "hichain_iso_session_key")`
   - **第 2 步**：手机回 `token = HMAC-SHA256(psk, rand_self || rand_peer || peerAuthId || selfAuthId)`；手表回 `returnCodeMac = HMAC-SHA256(psk, 00000000)`。
   - **第 3 步（仅 op1）**：手机用 `isoKey` + 12 字节 nonce + AAD `"hichain_iso_exchange"` 加密 16 字节 challenge；手表回 `encAuthToken` 和 `nonce`，AAD 为 challenge，解出**永久 authToken**。
   - **第 4 步**：手机用 AAD `"hichain_iso_result"` 加密 `00000000`；手表返回 `0x04 = 0x01` 表示结束。
   - op2 结束后会话密钥：`sessionKey = HKDF-SHA256(isoKey, rand_self || rand_peer, "hichain_return_key")`。
   - 本项目把 op1 得到的 authToken 按 BLE 地址保存在本地，下次直接用 op2，不再需要 PIN，也不会再弹窗。

JSON 使用紧凑格式（无空格），字段顺序与参考实现一致；`message` 字段在 op2 时或上 `0x10`，并带 `isDeviceLevel: false`。

### 4b. 配对（HiChain Lite，设备类型 1–3）

本机先生成一个随机 16 字节密钥 `secretKey`（首次配对时生成，之后按 BLE 地址保存；它就是后续的会话密钥）。

1. `0x01/0x2C` PinCode：同上，用固定 digest secret 解出 PIN（authVersion 2 时跳过此步）。
2. `0x01/0x13` Auth（不加密）
   - 请求：`0x01` challenge(32)、`0x02` authVersion(BE16) + clientNonce(16)、`0x03` authAlgo。
   - `digest(message, key, nonce)`：
     - `hashKey = SHA256(hexUpper(key))`，把 `hashKey[i]` 异或进 digest secret（按 authVersion 选 V1/V2/V3）。
     - `msg = secret(16) || message(2)`；`authAlgo == 1` 时 `step = PBKDF2-SHA256(hexUpper(msg), hexUpper(nonce), 1000, 32)`，否则 `step = HMAC-SHA256(msg, nonce)`。
     - 返回 `HMAC-SHA256(step, nonce) || step`（64 字节）。
   - challenge 用 `message = 0x01 0x00`，`key` = PIN（authVersion 2 时用 secretKey），`nonce = serverNonce || clientNonce`；取前 32 字节发送，`[32:48]` 作为 `firstKey`。
   - 响应 `0x01` 应等于 `digest(0x01 0x10, ...)[:32]`。
3. `0x01/0x0F` BondParams（不加密）
   - 请求：`0x01` 空、`0x03` 序列号（6 字节 ASCII，取自本机 MAC 去掉冒号后第 6–12 位）、`0x04 0x02`、`0x05` 空、`0x07` 本机 MAC（17 字节 ASCII）、`0x09` 空。
   - 响应：`0x01` 状态、`0x09` 加密计数器（BE32）。
4. `0x01/0x0E` Bond（不加密）
   - `iv = random(12) || counter(BE32)`，counter 为上一步的值 +1。
   - `wrapped = AES-CBC(secretKey, key = firstKey, iv)`（GCM 机型用 AES-GCM）。
   - 请求：`0x01` 空、`0x03 0x00`、`0x05` 序列号、`0x06` wrapped、`0x07` iv。
   - 此后 `sessionKey = secretKey`，加密计数器从 iv 尾部继续递增。

本机 MAC 由持久化的 device id 推导，保证每次连接手表看到的是同一个对端。

## 5. 传输加密

配对完成后，除少数握手包外，所有请求/响应的 body 都替换为：

```
TLV: 0x7C = 0x01, 0x7D = IV(16), 0x7E = ciphertext
```

- GCM：AES-GCM，12 或 16 字节 IV，密文末尾带 16 字节 tag，无 AAD。
- CBC：AES-CBC + PKCS#7，16 字节 IV。发送时 IV 为 `random(12) || counter(BE32)`，counter 从 BondParams 响应开始逐包递增（HiChain Lite）；GCM 机型用随机 IV。
- Gadgetbridge 与 HiChain Lite 机型都使用“12 字节随机 + 4 字节计数器”的 IV；计数器让手表能识别重放并保持包序。

## 6. 文件上传（服务 0x28）

文件类型：表盘 1、**音乐 2**、背景图 3、应用 6、固件 7。

流程由手表驱动：

| 步骤 | 方向 | 命令 | 内容 |
| --- | --- | --- | --- |
| 1 | 手机 → 手表 | `0x28/0x02` | `0x01` 文件名、`0x02` 文件大小 BE32、`0x03` 类型 |
| 2 | 手表 → 手机 | `0x28/0x02` | `0x7F` 结果码（非 100000 即失败） |
| 3 | 手表 → 手机 | `0x28/0x03` | `0x01` fileId，要求 SHA-256 |
| 4 | 手机 → 手表 | `0x28/0x03` | `0x01` fileId、`0x03` SHA-256(文件) |
| 5 | 手表 → 手机 | `0x28/0x04` | `0x01` fileId、`0x05` unitSize、`0x09` 是否免加密 |
| 6 | 手机 → 手表 | `0x28/0x04` | `0x7F` 成功、`0x01` fileId、（可选）`0x09 0x01` |
| 7 | 手表 → 手机 | `0x28/0x05` | `0x02` offset BE32、`0x03` 请求长度 BE32 |
| 8 | 手机 → 手表 | `0x28/0x06` | 裸数据块（见下），可能再套传输加密和分片 |
| 9 | 重复 7–8 | | 直到发完 |
| 10 | 手表 → 手机 | `0x28/0x07` | `0x01` fileId，上传成功 |
| 11 | 手机 → 手表 | `0x28/0x07` | `0x7F` 成功、`0x01` fileId |
| 12 | 手表 → 手机 | `0x25/0x09` | `0x01` index、`0x02` 文件名，要求歌名/歌手 |
| 13 | 手机 → 手表 | `0x25/0x09` | `0x01` index、`0x03` 标题、`0x04` 歌手 |

数据块格式（`0x28/0x06`）：

```
fileId (1) | index (1) | offset (BE32) | data (unitSize 字节)
```

`index` 是本次请求内块的序号，从 0 开始；`offset` 是该块在文件中的绝对偏移。`unitSize` 由手表在 `0x28/0x04` 里给出（典型 200–244）。若块体加上帧头后超过分片大小，需要再按帧格式分片。

速度相关的实现细节：

- 单帧上限取 `ATT MTU - 3`（Windows 上由 `BleakClient.mtu_size` 读取），不再固定 244B；
  大 MTU 适配器因此能把一个 `unitSize` 放进更少的帧。
- 手表的 `0x28/0x05` 只给“下一次要多少字节、从哪开始”，节奏完全由固件决定；
  手机侧只能把这一段尽量连续地写出去（本项目按窗口并发提交写入，失败时退回逐帧发送）。
- 手表没有“中止上传”命令。取消上传时只能停止发送并丢弃后续的 `0x28/0x05`，
  手表通常要等自己的超时才会恢复；残留的未完成文件由手表自行丢弃或留给用户删除。
- 真正决定吞吐的是**连接间隔**，不是 MTU。手表的 `0x28/0x05` 一次往返只能换来一段数据，
  每帧写入又都要等下一个连接事件；间隔 30ms 时，即使 MTU 514 也只有十几 KB/s。
  Android 上的官方 App 调用 `BluetoothGatt.requestConnectionPriority(CONNECTION_PRIORITY_HIGH)`
  把间隔压到 11.25–15ms；Windows 11 的等价 API 是
  `BluetoothLEDevice.RequestPreferredConnectionParameters(ThroughputOptimized)`
  （UniversalApiContract v14），本项目在连接后立即请求，断开时释放。
- 写入类型同样关键：`Write Request`（带响应）每个连接事件只能发一包，
  `Write Command`（无响应）可以连发多包。FE01 支持无响应写入时，开启「快速模式」能再快几倍。
- 每个 `0x28/0x06` 只能携带一个 `unitSize` 的数据，所以单帧利用率 =
  `unitSize / min(手表分片, MTU - 3)`。`unitSize` 明显小于 MTU 时，剩下的空间是浪费的，
  但协议要求按手表的 `unitSize` 切分，不能自行合并。

失败时手表发 `0x28/0x08`，`0x7F` 为错误码：

| 错误码 | 含义 |
| --- | --- |
| 140004 | 文件/表盘数量已达上限 |
| 140008 | 文件已存在 |
| 140009 | 存储空间不足 |

## 7. 音乐信息（服务 0x25）

- `0x25/0x04` 存储信息
  - 请求：`0x01 0x02 0x03 0x04 0x05`（空值）
  - 响应：`0x01` 歌曲帧数、`0x02` 剩余空间（MB）、`0x03` 支持格式位图、`0x04` 最大曲目数、`0x05` 当前曲目数。
  - 格式位图按字节展开：byte0 = mp3/wav/aac/sbc/msbc/hwa/cvsd，byte1 = pcm/ape/m4a/flac/opus/ogg/butt，byte2 = amr/imy；最高位为 0 表示后面没有更多字节。
- `0x25/0x05` 歌曲列表
  - 请求：`0x01` startFrame BE16、`0x04` endIndex BE16（一次性读取可发 `0, 0xFFFF`）。
  - 响应：`0x01`/`0x04` 回显范围，`0x82` 容器内是多个 `0x83` 子 TLV：`0x04` index、`0x05` 标题、`0x06` 歌手、`0x07` 文件名。
  - 少数固件一次只返回一首，需要递增 startFrame 继续请求。
- `0x25/0x09` 歌曲元数据：见上传流程第 12–13 步。
- `0x25/0x01` 手表打开音乐界面；`0x25/0x02` 手机推送播放信息；`0x25/0x03` 手表按键/音量。本项目只做应答，不实现完整的媒体控制。

## 8. 已知差异与限制

- 本项目实现 HiChain 与 HiChain Lite 两种配对；更老的“普通模式”设备暂不支持。
- Windows 的 BLE MTU 由系统协商，`bleak` 通过 `mtu_size` 暴露；若某次写入仍报长度错误，会按失败自动缩小分片重试。
- 首次配对的确认弹窗由手表固件控制，超时时间已放宽到 45–90 秒。
- 不实现删除单曲：参考实现只提供歌单操作（`0x25/0x08`），没有确认的单曲删除命令。
- 协议随固件可能变化，遇到问题请导出诊断日志反馈。
