1. 项目简介
原名：ljwthgnd（取自 "LuaJIT What The Hell is Going On" 的缩写，意思是"LuaJIT 这到底是什么鬼"反编译器）
命名规则：沿用了 LuaJIT C 源码的变量命名风格
2. 两个警告
WARNING 1：代码还没写完、没测试完，不保证反编译出来的代码接近原始代码，用自负风险
WARNING 2：这整个项目目前还是个原型，正式版应该用 Lua 自己写（这样就能反编译反编译器本身，是个很好的测试）
3. 来源与免责声明
核心代码（反编译器本身 + 测试文件）是从网上获取的，无法验证确切来源
本项目 fork 新增的部分只有：gui.py（GUI界面）、decompiler_api.py（API层）、baseline_semantic.py（语义回归基线）、以及配套的 AST/unwarper/writer 修复
其他都是无法验证的原始代码
工具仅供互操作性、研究和教育用途，反编译可能涉及版权法，用户需自行确保有权处理字节码
