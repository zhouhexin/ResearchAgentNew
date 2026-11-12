from tools.document_parser import parse_pdf

document = parse_pdf("../data/pdfs/关于调整更新部分机构公文版头和发文字号的通知.pdf")

print("PDF 页数：", document.page_count)
print("正文长度：", len(document.text))
print("文本片段数：", len(document.chunks))

first = document.chunks[0]

print("第一页片段：")
print(first.text[:300])
print("页码：", first.page_number)

assert document.text[
    first.start_char:first.end_char
] == first.text