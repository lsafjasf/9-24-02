# varbit — 变长整数与位打包编解码库

纯 Python 3 标准库实现，核心只用移位/掩码位运算，不依赖任意精度整数的现成转换。

## 位级格式定义

- **位序**：字节内 MSB 优先（网络位序），字段可起于任意位偏移开始，不要求字节对齐。
- **定宽字段**：1–64 位，大端位序。
- **无符号变长整数**：8 位一组 = 1 位续位标志（MSB）+ 7 位载荷；载荷组间小端
  （第一组是最低 7 位）。字节对齐时即 LEB128。**最短编码为唯一合法编码**：
  终止组必须非零（值 0 本身除外），接收方抛出 `NonCanonicalError` 并给出
  多余字节数 `extra_bytes`。
- **有符号变长整数**：zigzag 映射（0→0, -1→1, 1→2, …）叠加在无符号变长整数之上，
  全库一致使用此一种方案。
- **溢出防护**：默认载荷上限 64 位（`max_bits` 可调）。超过 10 组的续位前缀
  （人为构造的超长前缀）立即抛 `VarintOverflow`，不会无限等待。

## 流式解码

`BitReader.feed(data, bit_offset=0, bit_length=None)` 接受在**任意位边界**被切断的
分块输入（可指定块内起始位偏移与位长度）。数据不足时抛 `NeedMoreBits`，其
`needed` 属性报告还缺多少位；读游标不动，喂入更多数据后原样重试即可。
分块解码结果与一次性解码逐位一致（有测试保证）。

## 文件

| 文件 | 说明 |
|---|---|
| `varbit.py` | 库本体：`BitWriter` / `BitReader` / zigzag / 异常类型 |
| `refimpl.py` | 逐位参考实现（bit 列表，结构独立，仅用于对拍） |
| `test_varbit.py` | 单元测试 + 位级对拍 + 流式一致性 + 体积对比 |

## 运行

```sh
python3 -m unittest -v test_varbit      # 全部测试（含对拍与体积对比表）
python3 test_varbit.py                  # 同上
```

## 快速上手

```python
from varbit import BitWriter, BitReader, NeedMoreBits

w = BitWriter()
w.write_bits(0b101, 3)        # 3 位标志位
w.write_uvarint(300)          # 无符号变长
w.write_svarint(-12345)       # 有符号（zigzag）
data, nbits = w.tobytes(), w.bit_length

r = BitReader()
r.feed(data, 0, nbits)        # 可分多块、按任意位边界喂入
assert r.read_bits(3) == 0b101
assert r.read_uvarint() == 300
assert r.read_svarint() == -12345
```

## 编码体积对比（1000 个随机整数，实测）

| 分布 | varint | 定长 64 位 | 比值 |
|---|---|---|---|
| 0..9 | 1000 B | 8000 B | 0.12 |
| u8 | 1531 B | 8000 B | 0.19 |
| u32 | 4942 B | 8000 B | 0.62 |
| u64 | 9494 B | 8000 B | 1.19 |
| -50..50（zigzag） | 1000 B | 8000 B | 0.12 |
