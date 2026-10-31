import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import image_kind as ik

K = ik.ImageKind


class TestClassifyText(unittest.TestCase):
    """用真實 RapidOCR 輸出 (圖庫與隨機樣本) 當測資，包含 OCR 的常見誤認"""

    def check(self, text, category, slots=(), festival=None):
        kind = ik.classify_text(text)
        self.assertEqual(kind, K(category, tuple(slots), festival), text)

    def test_time_slot_greetings(self):
        self.check("FORMOSA!早安悠然自得喜樂相随", "greeting", ["morning"])
        self.check("美麗好心情美好的一天午安FORMOSA!", "greeting", ["noon"])
        self.check("FORMOSA!晚安Taiwan夜幕低垂享受寧美好", "greeting", ["evening"])
        self.check("早安 Good Morninq事滿事事順利FORMOSA!", "greeting", ["morning"])
        self.check("GoodNight寧静夜晚你好萝FORMOSA!", "greeting", ["evening"])

    def test_ocr_misreads_are_tolerated(self):
        self.check("晚按夜晚舒心幸福温馨FORMOSA!", "greeting", ["evening"])               # 晚安 -> 晚按
        self.check("FORMOSA!Merning麥寮生工業園區一聲早，再聲好開心如意", "greeting", ["morning"])   # Morning -> Merning
        self.check("MORNING以正向熊度迎接新的開始！FORMOSA!", "greeting", ["morning"])        # 只有英文
        self.check("FORMOSA!早安幸福飞揚", "greeting", ["morning"])                         # 簡繁混用

    def test_weak_dawn_evidence_only_counts_without_a_strong_greeting(self):
        self.assertEqual(ik.find_slots("晨曦咖啡美好"), ("morning",))                        # 只有「晨」
        self.assertEqual(ik.find_slots("晚安晨光"), ("evening",))                           # 有明確的晚安：晨字不算早安

    def test_greeting_without_a_specific_slot(self):
        kind = ik.classify_text("FORMOSA!日安祝福您士安樂")
        self.assertEqual(kind, K("greeting", ()))
        self.assertTrue(kind.is_time_neutral)                                              # 沒有時段，等於無時段關聯

    def test_festivals_with_their_names(self):
        self.check("中秋節快樂闔家團圓", "festival", festival="中秋節")
        self.check("父親節快樂", "festival", festival="父親節")
        self.check("端午安康", "festival", festival="端午節")
        self.check("恭賀新禧萬事如意", "festival", festival="春節")
        self.check("平安喜樂聖证快樂FORMOSA!", "festival", festival="聖誕節")              # 誕 -> 证
        self.check("母亲节快乐", "festival", festival="母親節")                             # 簡體

    def test_english_festival_names(self):
        # 真機實測：機器人曾回一張「HAPPY Mother's Day 與媽媽歡聚幸福時刻」，因為只認中文被當成普通祝福圖
        self.check("HAPPY Mother's Day 與媽媽歡聚幸福時刻", "festival", festival="母親節")
        self.check("HAPPYMothersDay", "festival", festival="母親節")                       # 空白與撇號被吃掉或換成別的符號
        self.check("Happy Father's Day", "festival", festival="父親節")
        self.check("MERRY CHRISTMAS 幸福氣息繞著你", "festival", festival="聖誕節")
        self.check("Christmas Eve 平安夜", "festival", festival="聖誕節")                        # 沒有 Merry 也算
        self.check("Valentine's Day", "festival", festival="情人節")
        self.check("Happy Mid-Autumn Festival", "festival", festival="中秋節")
        self.check("Happy New Year 萬事如意", "festival", festival="春節")
        self.check("Halloween", "festival", festival="萬聖節")

    def test_happy_holiday_is_still_just_a_blessing(self):
        self.check("FORMOSA!HappyHoliday幸福無所不在", "blessing")                       # 「假日」不是節日

    def test_festival_wins_over_greeting(self):
        self.check("父親節早安祝福", "festival", festival="父親節")                          # 「父親節早安」是父親節的圖

    def test_care_blessing_and_inspire(self):
        self.check("雨天路滑請慢行注意安全多留心FORMOSA!", "care")
        self.check("平安最重要氯温下降 天雨路滑FORMOSA!", "care")
        self.check("齊心在家FORMOSA!團結抗疫", "care")
        self.check("FORMOSA!假日愉快趁著好天氣出門去旅行", "blessing")
        self.check("福氣自來FORMOSA!", "blessing")
        self.check("FORMOSA!HappyHoliday幸福無所不在City", "blessing")
        self.check("就失去60秒的快樂生氣1分鐘不要生氣EIKFORMOSA!", "inspire")
        self.check("安漫漫人生路事事都万福FORMOSA!", "inspire")

    def test_holiday_spelling_errors_still_count(self):
        self.check("FORMOSA!CityHappyHtoliday台灣宜蘭北橫明池", "blessing")

    def test_unrecognised_text_is_none(self):
        for text in ("", "   ", "FORMOSA!", "ABC123", "保證品質"):
            self.assertIsNone(ik.classify_text(text), text)                                # 呼叫端要捨棄這類圖
        self.assertIsNone(ik.classify_text(None))

    def test_ties_prefer_care_then_inspire_then_blessing(self):
        self.assertEqual(ik.classify_text("注意幸福").category, "care")
        self.assertEqual(ik.classify_text("努力幸福").category, "inspire")

    def test_festival_vs_substrings(self):
        self.assertIsNone(ik.find_festival("保證品質"))                                     # 「證」不會被誤判成聖誕
        self.assertEqual(ik.find_festival("中秋節快樂 父親節"), "中秋節")                     # 命中較多的優先 (同分取表中先出現的)


class TestCampaign(unittest.TestCase):
    """候選人、競選、政黨相關的圖一律不回覆 (自動回祝福圖會像是在附和政治宣傳)"""

    def test_campaign_texts(self):
        for text in ("3號議員候選人王大明全力衝刺", "國民黨市長候選人請支持", "請投票給我", "選舉必勝",
                     "總統候選人", "支持5號當選", "里長伯與您同在", "民進黨", "民眾黨", "拜託鄉親支持連任",
                     "早安支持3號議員候選人",                      # 夾在問候裡也算
                     "3號王大明員候選人全力衢刺請支持",              # 真實 OCR 輸出：認錯幾個字仍然攔得住
                     "请投票给我", "选举必胜", "总统候选人", "民进党"):   # 簡體
            self.assertTrue(ik.is_campaign_text(text), text)
            self.assertIsNone(ik.classify_text(text), text)

    def test_real_ocr_misreads_of_campaign_posters(self):
        # 來自真實宣傳圖的 OCR 輸出，人名與電話已換成化名。重點是 OCR 常見的讀錯字形：
        # 「候選人」讀成候送人 / 候道人 / 候理人 / 候遇人 / 候进人，「市議員」讀成市員 / 北市員
        for text in ("中和區東南里里長候選人①王大明用心服務請支持",
                     "新北市員候进人3陳某某敢請支持連任",
                     "新北市員金某某",                                  # 只剩頭銜被讀錯的字
                     "造福鄉里手同行里長候遇人王大明競選服務處成立大會邀請函",
                     "敢講會做拼服務團结东南共創更好的未來候道人王大明",
                     "武真雄打拼新北市長候理人1王大明望你牵成",
                     "團结东南專業服務創新幸福东南候送人王大明里里長",
                     "3號王大明服務處成立大會"):
            self.assertTrue(ik.is_campaign_text(text), text)
            self.assertIsNone(ik.classify_text(text), text)

    def test_similar_looking_words_in_normal_blessings_are_not_campaign(self):
        for text in ("造福鄉里平安喜樂", "問候親人早安幸福快樂", "守候家人幸福美滿", "等候好消息事事順利",
                     "公里數健康步行", "祝您幸福快樂每一天", "服務大眾平安喜樂", "感謝您的支持"):
            self.assertFalse(ik.is_campaign_text(text), text)

    def test_blessings_that_merely_mention_similar_words_are_not_campaign(self):
        for text in ("祝您當選幸福人生", "早安祝您心想事成", "平安喜樂", "投資理財有道", "中秋節快樂闔家團圓",
                     "福氣自來", "早安美好時光", ""):
            self.assertFalse(ik.is_campaign_text(text), text)
        self.assertEqual(ik.classify_text("祝您當選幸福人生平安喜樂").category, "blessing")

    def test_campaign_wins_over_every_other_category(self):
        self.assertIsNone(ik.classify_text("中秋節快樂 市長與您團圓"))                      # 節日圖掛了市長頭銜
        self.assertIsNone(ik.classify_text("晚安 議員關心您"))
        self.assertIsNone(ik.classify_text("雨天路滑注意安全 里長提醒"))

    def test_normalisation_does_not_break_the_title(self):
        self.assertEqual(ik.normalize_text("里長"), "里長")                                 # 「里」是正體字，不能被當成簡體的「裡」

    def test_ordinary_scores_unchanged(self):
        self.assertEqual(ik.classify_text("早安 美好時光"), ik.ImageKind("greeting", ("morning",)))


class TestImageKind(unittest.TestCase):
    def test_label_and_time_neutral(self):
        self.assertEqual(K("festival", (), "中秋節").label, "節日(中秋節)")
        self.assertEqual(K("greeting", ("morning",)).label, "時段問候(morning)")
        self.assertEqual(K("care").label, "平安叮嚀")
        self.assertFalse(K("festival", (), "中秋節").is_time_neutral)                       # 節日圖是特定日子的圖
        self.assertFalse(K("greeting", ("evening",)).is_time_neutral)
        for category in ("care", "blessing", "inspire"):
            self.assertTrue(K(category).is_time_neutral)

    def test_dict_round_trip_and_validation(self):
        for kind in (K("festival", (), "端午節"), K("greeting", ("noon", "evening")), K("inspire")):
            self.assertEqual(K.from_dict(kind.to_dict()), kind)
        self.assertIsNone(K.from_dict({"category": "nope"}))
        self.assertIsNone(K.from_dict({"category": "greeting", "slots": ["midnight"]}))
        self.assertIsNone(K.from_dict({"category": "festival", "festival": "不存在的節日"}))
        self.assertIsNone(K.from_dict(None))
        self.assertIsNone(K.from_dict({}))

    def test_normalize_text(self):
        self.assertEqual(ik.normalize_text(" 早 安\n快乐 "), "早安快樂")


if __name__ == "__main__":
    unittest.main()
