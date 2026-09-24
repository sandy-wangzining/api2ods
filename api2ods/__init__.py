# -*- coding: utf-8 -*-
"""api2ods —— 通用 REST API → MaxCompute ODS（裸 json 列 + pt 分区）。

使用方式：
    python -m api2ods --job jobs/xxx.json ...     # 源码目录直接跑
    api2ods --job jobs/xxx.json ...               # pip/pipx 安装后（console script）
"""

VERSION = "2.1.8"
