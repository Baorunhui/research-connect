import type { CardTemplate, CardData, PageRenderData } from "../base"

// Article 公众号长文风格模板
// 参考截图：米白背景 + 右侧水印植物纹 + 顶部字数/阅读时间 + 超大粗体标题 + 正文段落

const defaultData: CardData = {
  title: "我们为什么越来越难以集中注意力",
  pages: [
    "<p>神经科学家给这种现象起了个名字：&ldquo;注意力残留&rdquo;。当你从一件事切换到另一件事，前一件事的思绪并不会立刻消散，而是像幽灵一样悄悄跟随，侵占你本该全力投入的当下。</p><p>更糟糕的是，我们已经把这种分心状态内化成了&ldquo;正常&rdquo;。打开电脑的第一件事是看通知，吃饭时眼睛盯着屏幕，睡前还要刷完&ldquo;最后一条&rdquo;推送。</p><p>专注力并不是天赋，而是一种肌肉——不用则废，越练越强。问题在于，我们已经太久没有给它真正的锻炼机会了。</p>",
  ],
  tags: ["专注", "认知", "数字时代"],
}

// 阅读时间（中文约300字/分钟）
function readMinutes(charCount: number): number {
  return Math.max(1, Math.round(charCount / 300))
}

// 右侧水印植物 SVG（半透明淡灰）
function WatermarkPlant() {
  return (
    <svg
      width={200}
      height={320}
      viewBox="0 0 200 320"
      fill="none"
      style={{ position: "absolute", right: -20, top: 20, opacity: 0.07, pointerEvents: "none" }}
    >
      {/* 主茎 */}
      <path d="M100 320 C100 280 95 240 100 180 C105 120 98 80 105 20" stroke="#4A4A4A" strokeWidth={2.5} fill="none" strokeLinecap="round"/>
      {/* 左侧大叶 */}
      <path d="M100 200 C80 185 55 170 40 148 C60 142 85 158 100 180" fill="#4A4A4A"/>
      <path d="M100 200 C80 185 55 170 40 148" stroke="#4A4A4A" strokeWidth={1.2} fill="none"/>
      {/* 右侧大叶 */}
      <path d="M100 170 C118 152 142 140 162 120 C148 136 122 150 100 170" fill="#4A4A4A"/>
      <path d="M100 170 C118 152 142 140 162 120" stroke="#4A4A4A" strokeWidth={1.2} fill="none"/>
      {/* 左侧中叶 */}
      <path d="M100 140 C78 128 58 112 44 90 C66 92 88 110 100 140" fill="#4A4A4A"/>
      {/* 右侧中叶 */}
      <path d="M102 115 C120 100 144 92 165 78 C148 96 124 106 102 115" fill="#4A4A4A"/>
      {/* 左小叶 */}
      <path d="M103 80 C88 68 72 58 62 42 C78 48 96 62 103 80" fill="#4A4A4A"/>
      {/* 右小叶 */}
      <path d="M105 55 C118 42 134 34 152 24 C138 36 120 46 105 55" fill="#4A4A4A"/>
      {/* 顶端小芽 */}
      <path d="M105 20 C110 10 118 4 126 0 C118 8 110 14 105 20" fill="#4A4A4A"/>
      {/* 底部根部装饰 */}
      <path d="M100 300 C88 295 72 292 60 285" stroke="#4A4A4A" strokeWidth={1.5} fill="none" strokeLinecap="round"/>
      <path d="M100 300 C112 294 128 290 140 282" stroke="#4A4A4A" strokeWidth={1.5} fill="none" strokeLinecap="round"/>
    </svg>
  )
}

function ArticleCard({ data }: { data: PageRenderData }) {
  const contentText = data.content.replace(/<[^>]*>/g, "").trim()
  const isTitleOnly = !!data.title && !contentText

  // 直接使用 buildPages 注入的全文总字数
  const charCount = data.totalCharCount
  const mins = readMinutes(charCount)

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        position: "relative",
        overflow: "hidden",
        // 米白暖白背景
        background: "#F7F5F0",
        display: "flex",
        flexDirection: "column",
        padding: "32px 36px 28px",
        fontFamily: '"PingFang SC", "Hiragino Sans GB", "Noto Sans SC", "Source Han Sans", sans-serif',
      }}
    >
      {/* 右侧植物水印 */}
      <WatermarkPlant />

      {/* 顶部：字数 + 阅读时间 */}
      <div
        style={{
          fontSize: 12,
          color: "#9A9590",
          letterSpacing: 0.3,
          marginBottom: isTitleOnly ? 32 : 24,
          flexShrink: 0,
          fontFamily: '"PingFang SC", sans-serif',
        }}
      >
        全文{charCount}字&nbsp;&nbsp;|&nbsp;&nbsp;阅读约{mins}分钟
      </div>

      {/* 大标题 */}
      {data.title && (
        <h1
          style={{
            fontSize: isTitleOnly ? 46 : 40,
            fontWeight: 900,
            color: "#141414",
            lineHeight: 1.28,
            margin: "0 0 28px",
            letterSpacing: -0.5,
            fontFamily: '"PingFang SC", "Hiragino Sans GB", "Noto Sans SC", sans-serif',
            wordBreak: "break-all",
            flexShrink: 0,
            // 仅标题时居中偏下
            ...(isTitleOnly ? {
              marginTop: "auto",
              marginBottom: "auto",
              paddingBottom: "15%",
            } : {}),
          }}
        >
          {data.title}
        </h1>
      )}

      {/* 正文 */}
      {!isTitleOnly && (
        <>
          <div
            className="tpl-article-body"
            style={{ flex: 1, overflow: "hidden", position: "relative", zIndex: 1 }}
            dangerouslySetInnerHTML={{ __html: data.content }}
          />

          {/* 标签 */}
          {data.tags.length > 0 && (
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 18, flexShrink: 0 }}>
              {data.tags.map(tag => (
                <span key={tag} style={{
                  fontSize: 11,
                  color: "#9A9590",
                  letterSpacing: 0.3,
                }}># {tag}</span>
              ))}
            </div>
          )}
        </>
      )}

      {/* 页码 */}
      {data.pageInfo && (
        <div style={{
          position: "absolute",
          bottom: 22,
          right: 36,
          fontSize: 11,
          color: "#B8B4AE",
          letterSpacing: 1,
        }}>
          {data.pageInfo.current} / {data.pageInfo.total}
        </div>
      )}

      {/* 底部细分割线 */}
      <div style={{
        position: "absolute",
        bottom: 0,
        left: 36,
        right: 36,
        height: 1,
        background: "rgba(0,0,0,0.06)",
      }}/>
    </div>
  )
}

export const articleTemplate: CardTemplate = {
  id: "article",
  name: "公众号长文",
  description: "米白背景 + 植物水印，超大粗体标题，严肃阅读氛围",
  render: (data) => <ArticleCard data={data} />,
  defaultData,
}
