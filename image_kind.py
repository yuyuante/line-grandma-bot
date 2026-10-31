"""
從圖片上的文字判斷長輩圖的「性質」：節日 (哪個節日)、時段問候 (早/午/晚安)、平安叮嚀、祝福鼓勵、心靈雞湯。
判斷不出來回傳 None (呼叫端一律捨棄：不入庫、不回覆)。

候選人、競選、政黨相關的圖一律判為 None，優先於所有分類：自動回一張祝福圖，看起來像在回應或附和政治宣傳。
這是關鍵字規則，OCR 讀不出字的圖，或文字很隱晦的宣傳圖攔不住。

規則與優先順序：
0. 競選相關 (見 is_campaign_text)：不回覆。
1. 節日：文字含節日名稱 (父親節、中秋、聖誕...)，記下是哪個節日。節日圖優先，因為「父親節早安」是父親節的圖。
2. 時段問候：早安 / 午安 / 晚安 (含英文 Good Morning / Good Night)，記下時段。
3. 其餘三類看關鍵字命中數，最多的勝出，同分依 平安叮嚀 > 心靈雞湯 > 祝福鼓勵。

OCR 常見的誤差都要容忍：簡繁混用 (顺心、愉悦)、認錯字 (晚按、Merning、聖證)。
關鍵字是用 30 張隨機圖加圖庫 44 張的 RapidOCR 實際輸出校準的。
"""
import re
from dataclasses import dataclass

CATEGORY_NAMES = {
    "festival": "節日",
    "greeting": "時段問候",
    "care": "平安叮嚀",
    "blessing": "祝福鼓勵",
    "inspire": "心靈雞湯",
}
SLOTS = ("morning", "noon", "evening")

# OCR 輸出簡繁混用，先把常見簡體字換成繁體再比對 (只收關鍵字用得到的字)
_SIMPLE_TO_TRADITIONAL = str.maketrans({
    "乐": "樂", "顺": "順", "开": "開", "气": "氣", "温": "溫", "态": "態", "烦": "煩", "恼": "惱", "梦": "夢",
    "坚": "堅", "学": "學", "运": "運", "满": "滿", "诞": "誕", "贺": "賀", "节": "節",
    "亲": "親", "团": "團", "圆": "圓", "悦": "悅", "觉": "覺", "难": "難", "过": "過", "风": "風", "雾": "霧",
    "饮": "飲", "暖": "暖", "护": "護", "岁": "歲", "钟": "鐘", "宁": "寧", "静": "靜", "华": "華", "实": "實",
    "为": "為", "们": "們", "时": "時", "间": "間", "还": "還", "体": "體", "关": "關", "爱": "愛", "对": "對",
    "会": "會", "来": "來", "后": "後", "没": "沒", "发": "發", "现": "現", "动": "動", "经": "經", "长": "長",
    "见": "見", "说": "說", "话": "話", "让": "讓", "应": "應", "该": "該", "总": "總", "带": "帶", "着": "著",
    "么": "麼", "点": "點", "热": "熱", "冷": "冷", "寒": "寒", "晒": "曬", "疗": "療", "营": "營",
    "养": "養", "众": "眾", "愿": "願", "阳": "陽", "辉": "輝", "红": "紅", "绿": "綠", "蓝": "藍", "黄": "黃",
    "闲": "閒", "释": "釋", "积": "積", "极": "極", "宽": "寬", "难": "難", "杂": "雜", "压": "壓", "钱": "錢",
    "烟": "煙", "禁": "禁", "识": "識", "认": "認", "试": "試", "虑": "慮", "战": "戰", "胜": "勝", "败": "敗",
    "忆": "憶", "怀": "懷", "亿": "億", "优": "優", "兴": "興", "师": "師", "园": "園", "书": "書", "课": "課",
    "苦": "苦", "随": "隨", "拥": "擁", "抱": "抱", "拼": "拼", "续": "續", "继": "繼", "结": "結", "终": "終", "绝": "絕",
    "选": "選", "举": "舉", "党": "黨", "议": "議", "员": "員", "统": "統", "进": "進", "县": "縣", "镇": "鎮",
    "乡": "鄉", "胜": "勝",
})

# 節日：標準名稱 -> 比對用的正規表示式 (已是繁體，含常見 OCR 誤認)
FESTIVALS = {
    "春節": r"春節|新年|新春|過年|賀歲|恭賀新禧|恭喜發財|年年有餘|HappyNewYear|LunarNewYear",
    "除夕": r"除夕|團圓飯",
    "元旦": r"元旦|跨年",
    "元宵節": r"元宵",
    "清明節": r"清明",
    "端午節": r"端午|粽子|龍舟|DragonBoat",
    "七夕": r"七夕",
    "中秋節": r"中秋|賞月|月餅|月圓人團|Mid.?Autumn",
    "重陽節": r"重陽",
    "父親節": r"父親節|爸爸節|Father.{0,2}s?Day",
    "母親節": r"母親節|媽媽節|Mother.{0,2}s?Day",
    "情人節": r"情人節|Valentine",
    "聖誕節": r"聖[誕蛋證证]|Merry?Christmas|Christmas|Xmas",
    "萬聖節": r"萬聖|Halloween",
    "教師節": r"教師節|Teacher.{0,2}s?Day",
    "兒童節": r"兒童節|Children.{0,2}s?Day",
    "勞動節": r"勞動節|勞工節|Labou?rDay",
    "國慶": r"國慶|雙十",
    "感恩節": r"感恩節|Thanksgiving",
    "冬至": r"冬至|湯圓",
    "畢業": r"畢業",
    "生日": r"生日快樂|生日|HappyBirthday",
    "結婚": r"新婚|結婚|婚禮|百年好合",
}

# 時段問候。強證據：明確的問候語；弱證據：只有「晨」字 (沒有任何強證據時才算早安)
_GREETING_STRONG = {
    "morning": r"早安|早上好|晨安|早啊|g[o0]{2}dm[oe]rn|m[oe]rning|m[oe]rnin",
    "noon": r"午安|中午好|下午好|g[o0]{2}dafternoon|afternoon",
    "evening": r"晚安|晚按|晚上好|夜安|好夢|好眠|g[o0]{2}dnigh|goodevening|晚好",
}
_GREETING_WEAK = {"morning": r"晨"}
_GREETING_NO_SLOT = r"日安"       # 「日安」是問候語但沒有特定時段

# 其餘三類的關鍵字 (命中幾個算幾分)
_CARE = r"注意|小心|保暖|保重|路滑|慢行|降溫|氣溫|天冷|天氣|寒流|防疫|抗疫|口罩|感染|叮嚀|提醒|安全|多喝水|防曬|中暑|駕駛|別忘|記得|切勿|防範|多穿|少吃|早睡|多運動"
_INSPIRE = r"人生|心態|態度|努力|堅持|放下|煩惱|學會|懂得|珍惜|夢想|勇敢|改變|成功|正能量|心境|知足|看開|包容|感恩|緣分|相遇|別人|自己|生氣|快樂的人|時間|歲月|機會|選擇|付出|擁有"
_BLESSING = r"幸福|快樂|祝福|順心|順利|平安|喜樂|吉祥|如意|開心|笑口常開|好運|福氣|萬事|心想事成|加油|愉快|假日|週末|周末|好心情|美好|健康|祈福|Happy.{0,3}oliday|平安是福|財源|發財|喜悅|滿滿|圓滿"


# 競選相關：明確的競選用語、政黨名稱，以及民選公職的頭銜 (長輩圖若掛著這些頭銜，多半是政治人物的宣傳)
_CAMPAIGN_STRONG = (r"候選人|候[選送道理遇進圾]人|參選|競選|連任|投票|選舉|選戰|拜票|助選|政見|造勢|藍白合|必勝|凍蒜|冬蒜|"
                    r"服務處|成立大會|請支持|懇請支持|敬請支持|"
                    r"國民黨|民進黨|民眾黨|時代力量|親民黨|新黨|台灣基進|綠黨")
_CAMPAIGN_TITLES = r"副總統|總統|立法委員|立委|縣長|市長|鄉長|鎮長|議員|里長|[市縣]員"      # 「市員」是 OCR 常把「市議員」讀掉一個字
# 「當選」單獨出現可能只是祝福話 (祝您當選幸福人生)，要搭配這些競選語境才算
_CAMPAIGN_ELECT = r"當選"
_CAMPAIGN_ELECT_CONTEXT = r"支持|號|請|拜託|感謝|全力|衝刺|選民|鄉親|連任|必勝|加油"


def is_campaign_text(text):
    """文字是不是候選人、競選、政黨相關的宣傳 (關鍵字規則)"""
    text = normalize_text(text)
    if not text:
        return False
    if re.search(_CAMPAIGN_STRONG, text) or re.search(_CAMPAIGN_TITLES, text):
        return True
    return bool(re.search(_CAMPAIGN_ELECT, text) and re.search(_CAMPAIGN_ELECT_CONTEXT, text))


@dataclass(frozen=True)
class ImageKind:
    category: str                    # festival / greeting / care / blessing / inspire
    slots: tuple = ()                # 時段問候的時段 (morning / noon / evening)，其他類為空
    festival: str = None             # 節日類的標準節日名稱

    @property
    def label(self):
        """給人看的名稱，例如「節日(中秋節)」「時段問候(morning)」"""
        name = CATEGORY_NAMES[self.category]
        detail = self.festival or ",".join(self.slots)
        return f"{name}({detail})" if detail else name

    def to_dict(self):
        return {"category": self.category, "slots": list(self.slots), "festival": self.festival}

    @staticmethod
    def from_dict(data):
        """從存檔還原；欄位不合法回傳 None"""
        try:
            category = data["category"]
            slots = tuple(data.get("slots") or ())
            festival = data.get("festival")
            if category not in CATEGORY_NAMES or any(s not in SLOTS for s in slots):
                return None
            if category == "festival" and festival not in FESTIVALS:
                return None
            return ImageKind(category, slots, festival)
        except Exception:
            return None

    @property
    def is_time_neutral(self):
        """無時段關聯：祝福、叮嚀、心靈雞湯，或沒有特定時段的問候。節日圖是特定日子的圖，不算"""
        if self.category == "festival":
            return False
        return self.category != "greeting" or not self.slots


def normalize_text(text):
    """去掉空白與換行、簡體換繁體，英文保留大小寫 (比對時各自處理)"""
    return re.sub(r"\s+", "", text or "").translate(_SIMPLE_TO_TRADITIONAL)


def _count(pattern, text):
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def find_festival(text):
    """回傳文字裡出現的節日標準名稱 (命中最多的那個)；沒有回傳 None"""
    text = normalize_text(text)
    best, best_n = None, 0
    for name, pattern in FESTIVALS.items():
        n = _count(pattern, text)
        if n > best_n:
            best, best_n = name, n
    return best


def find_slots(text):
    """回傳文字裡出現的問候時段 (依 morning / noon / evening 排序)，沒有回傳空 tuple"""
    text = normalize_text(text)
    found = [slot for slot in SLOTS if re.search(_GREETING_STRONG[slot], text, flags=re.IGNORECASE)]
    if not found:
        found = [slot for slot, pattern in _GREETING_WEAK.items() if re.search(pattern, text)]
    return tuple(found)


def classify_text(text):
    """
    由 OCR 文字判斷圖的性質，回傳 ImageKind；文字空白或認不出任何性質回傳 None (呼叫端要捨棄)。
    """
    text = normalize_text(text)
    if not text or is_campaign_text(text):
        return None
    festival = find_festival(text)
    if festival:
        return ImageKind("festival", (), festival)
    slots = find_slots(text)
    if slots or re.search(_GREETING_NO_SLOT, text):
        return ImageKind("greeting", slots)
    scores = {"care": _count(_CARE, text), "inspire": _count(_INSPIRE, text), "blessing": _count(_BLESSING, text)}
    best = max(scores.values())
    if best == 0:
        return None
    for category in ("care", "inspire", "blessing"):     # 同分的優先順序
        if scores[category] == best:
            return ImageKind(category)
