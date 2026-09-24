# varbit: 变长整数与位打包编解码库(Python 3, 仅标准库)

## 编码约定

- 位序: 字节内高位在前(MSB first), 多位整数按高位到低位写入位流。
- 位打包: `BitWriter.write_bits(value, width)` / `BitReader.read_bits(width)`,
  支持 1..64 位任意宽度, 写末尾不足一字节补 0。
- 变长整数: 无符号 LEB128(每字节 7 位载荷 + 续位), 只产生最短编码;
  解码端拒绝非最短编码(`NonShortestEncodingError.extra_bytes` 给出多余
  字节数), 拒绝超过 64 位的编码(`VarintOverflowError`)。
- 有符号: **ZigZag** 映射(`zigzag_encode`/`zigzag_decode`)后按无符号编码,
  全程 64 位语义(显式掩码, 不依赖 Python 任意精度)。
- 流式: `BitReader` 只依赖内存中已喂入的数据, 可用 `feed(bytes)` 或
  `feed_bits(value, width)` 在任意位边界分块喂入; 数据不足抛
  `InsufficientBits`, 其 `needed` 属性给出至少还需要的位数, 且不消费数据。

## 文件

- `varbit.py` — 库源码
- `reference.py` — 逐位参考实现(对拍基准)
- `bitdiff.py` — 位级对拍 + 编码体积对比脚本
- `test_streaming.py` — 单元测试: 边界值、最短编码拒绝、超长前缀、
  流式分块与整块读取一致性

## 运行

```sh
python3 test_streaming.py -v   # 单元测试
python3 bitdiff.py             # 位级对拍(默认 2000 轮) + 体积对比
python3 bitdiff.py 10000 7     # 指定轮数与随机种子
```
