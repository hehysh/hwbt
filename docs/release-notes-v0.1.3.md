GT 2e 这类较老的 GT 机型终于能通过配对这一步了。

## 这一版解决的问题

v0.1.2 修好写入方式后，链路参数、安全协商都通了，但卡在：

```
配对类型: 0x1
连接失败: 该手表使用不支持的配对方式 0x1（本工具只实现 HiChain）
```

`0x01` 不是错误，而是 **HiChain Lite**——Gadgetbridge 里给设备类型 1–3（GT 2e、
部分 GT 2、荣耀手表）用的另一套配对流程。之前只实现了新机型的 HiChain（ISO 握手），
所以直接放弃了。这一版补上了 HiChain Lite。

## HiChain Lite 流程（已实现并写了模拟手表测试）

1. `0x01/0x2C` 取 PIN（用固定 digest secret 解密，用户无需手输）
2. `0x01/0x13` 用 PIN 做挑战，`digest = HMAC(step, nonce) || step`；
   `authAlgo == 1` 时 `step` 用 PBKDF2-SHA256（1000 轮，32 字节）
3. `0x01/0x0F` BondParams：提交序列号 / 本机 MAC，取回加密计数器
4. `0x01/0x0E` Bond：用挑战里的 `firstKey` 加密本地生成的 16 字节会话密钥并提交
5. 之后所有报文用该密钥 AES-CBC 加密，IV = `random(12) || counter(BE32)`，
   计数器从 BondParams 返回值开始逐包递增（与 Gadgetbridge 一致）

本机 MAC / 序列号由持久化的 device id 推导，保证每次连接手表看到的是同一个对端。
配对密钥按 BLE 地址保存在 `%APPDATA%\HuaweiMusic\settings.json`，下次直接复用。

## 其它改动

- 日志不再写死「1/4 … 4/4」，改成阶段名，两种配对方式都能看懂进度
- 协议文档新增「4b. 配对（HiChain Lite）」完整字段说明
- README 的支持列表注明 GT 2e 走 HiChain Lite
- 91 项测试通过，其中新增：HiChain Lite 全流程配对、复用已保存密钥重连、
  Lite 链路下的完整上传、`digest_lite` 与 PBKDF2 分支、IV 计数器递增

## 使用顺序

1. 手表退出「连接新手机」二维码界面，回到表盘。
2. 手机：华为运动健康 → 设备 → 断开连接；仍不行就「解除配对」。
3. 如果之前在 Windows 里配过对：设置 → 蓝牙和其他设备 → 删除该手表。
4. 打开本程序 → 扫描 → 连接（**不要**在 Windows 里配对手表）。
   - 手表上如果出现 6 位配对码，点 ✓ 确认。

## 说明

- 这是第一次在真机上走到配对阶段，握手成功与否需要实测确认。
- 若失败，请勾选「调试日志」导出：日志里会显示卡在 PIN、认证、BondParams 还是 Bond。
