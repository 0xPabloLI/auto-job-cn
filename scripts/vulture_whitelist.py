# vulture 白名单：存在但「不读」的名字。vulture 不认接口一致性——fake/stub 的形参
# 必须与真实签名对齐（调用方按位置或关键字传参），形参本身不使用是设计而非死代码。
# 增删白名单条目须注释出处，防止白名单变成藏污区。

# _limits.handle_code 的分发签名形参（错误码处理表约定四参；#44 自 batch_apply_v2.py 迁出）
zp_data

# test_limits._Rng 的兼容形参（docstring「randint 取中值」的历史开关位；#44 自 test_batch_apply_v2 平移）
randint_mid

# test_live_apply FakePage.on / remove_listener —— 对齐 Playwright page API 签名
event

# test_platforms FakePage.wait_for_timeout(ms) / FakeKeyboard.type(text, delay=0)
# —— 对齐 Playwright API 签名，调用方可能以关键字传参，形参名不可改
ms
delay

# webui.Handler.log_message(fmt, *args) —— 标准库 BaseHTTPRequestHandler 覆写签名
fmt

# PlatformBase.apply / ChatFirstPlatform.apply（#44 后为 NotImplementedError 空接口）与
# lagou/liepin stub 的对齐形参——调用方按签名传参，形参本身不使用是设计
opts

# pii_scan._CandidateListing.is_file 的 follow_symlinks（#127）：对齐 pathlib.Path 的签名，
# 守卫只调 is_file()，这个内存载体恒真——形参名不可改（关键字调用），使用它是设计而非死代码
follow_symlinks
