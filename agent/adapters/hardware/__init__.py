"""Agent 硬件适配器（python-can / pyserial 等实现，P9.1 / §9.2）。

物理口的 open/close 由共享插件包 Agent 面在 ``execute`` 内自行完成；
Agent 框架只负责只读探测与互斥资源键声明（见 ``agent.drivers.base``）。
具体 CAN/Serial 收发 adapter 随 P9.2 真实插件落地。
"""
