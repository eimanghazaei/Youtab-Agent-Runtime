# طرح ضبط فشرده — دو گوینده (E001 و E002)

این طرح برای شرایطی است که فقط **دو** انسانِ داوطلب در دسترس است. هدف یک بستهٔ
**فشرده، ۱۵ تا ۲۰ دقیقه‌ای** برای هر نفر است، نه بستهٔ کامل ۵۱ تا ۷۱ دقیقه‌ای.
هر بخش مستقل و **قابل‌ازسرگیری** است؛ می‌توان وسط کار توقف کرد و بعداً از همان‌جا ادامه داد.

- **E001** → فقط برای **training**.
- **E002** → فقط برای **validation** (انتخاب آستانه/کاندید).
- محتوای ضبط هر دو نفر **یکسان** است. نقش (training/validation) به گوینده گفته نمی‌شود.
- فهرست عبارت‌های near-phrase **canonical** است و از `speaker_recording_spec.NEAR_PHRASE_ITEMS`
  گرفته شده؛ فقط **تعداد تکرار** برای فشرده‌سازی کم شده، نه خودِ فهرست.
- برنامهٔ ضبط (`recording_assistant`) نام و پوشهٔ هر فایل را **خودکار** می‌سازد؛ هیچ‌کس
  فایلی را دستی نام‌گذاری یا تکه‌تکه نمی‌کند.

فرمت ضبط: **WAV، mono، ۱۶٬۰۰۰ Hz یا بالاتر**. هیچ فایلی بعد از ضبط تبدیل/نرمال‌سازی/برش نمی‌شود.

مسیر خروجی (بیرون از git): `G:\Youtab-Wakeword-Human\incoming\E001` و `...\E002`
(معادل WSL: `/mnt/g/Youtab-Wakeword-Human/incoming/E001`).

---

## بخش ۱ — «Hey Youtab»، نزدیک به دستگاه، پنج نوع ادا (۱۰ ضبط)

عبارت در همهٔ این‌ها یکی است: **«Hey Youtab»**. هر حالت **۲ بار**.

| پوشه | حالت | تعداد |
|---|---|---|
| `positive_normal` | با صدای عادی و سرعت عادی | ۲ |
| `positive_slow` | آشکارا آهسته‌تر، ولی یک عبارت طبیعی | ۲ |
| `positive_fast` | آشکارا تندتر، مثل وقتی عجله داری | ۲ |
| `positive_quiet` | واقعاً آرام، مثل اینکه کسی را بیدار نکنی | ۲ |
| `positive_loud` | واقعاً بلند، مثل صدا زدن از آن‌سرِ اتاق (نه آنقدر که بشکند) | ۲ |

## بخش ۲ — فاصله و نویز واقعی (۹ ضبط)

باز هم عبارت **«Hey Youtab»**.

| پوشه | شرط | تعداد |
|---|---|---|
| `positive_farfield` | دست‌کم ۵ متر دور (اتاق مجاور با درِ باز)، صدای عادی | ۳ |
| `positive_noise_<A>` | منبع نویزِ A واقعاً روشن، نزدیک دستگاه | ۳ |
| `positive_noise_<B>` | منبع نویزِ B واقعاً روشن، نزدیک دستگاه | ۳ |

منابع نویز برای هر گوینده (برای پوشش متفاوت):
- **E001** → `positive_noise_tv` و `positive_noise_kitchen`
- **E002** → `positive_noise_street` و `positive_noise_fan`

## بخش ۳ — فهرست canonical عبارت‌های نزدیک (۳۴ ضبط، هرکدام ۱ بار)

هر عبارت را **طبیعی و یک‌نفس** بگو — **وسط اسم مکث نکن**. «Youtab» یک کلمه است.
دو ردیفِ نشان‌دارِ **[wake]** عبارتِ اصلی‌اند و باید فعال کنند؛ بقیه نباید فعال کنند —
آن‌ها را مثل جملهٔ عادی بگو، نه مثل تله. همه زیر پوشهٔ `near_phrase/` ذخیره می‌شوند.

| # | عبارت | # | عبارت |
|---|---|---|---|
| 1 | `hey you tab.` **[wake]** | 18 | `hey cab.` |
| 2 | `hey you tap.` | 19 | `hey you talk.` |
| 3 | `hey yoo tab.` **[wake]** | 20 | `a new tab.` |
| 4 | `okay youtab.` | 21 | `hey youtube.` |
| 5 | `okay tab.` | 22 | `hey you.` |
| 6 | `hey google.` | 23 | `eight.` |
| 7 | `hey siri.` | 24 | `two.` |
| 8 | `hey.` | 25 | `visual.` |
| 9 | `hey your tab.` | 26 | `four.` |
| 10 | `hey new tab.` | 27 | `zero.` |
| 11 | `hey utah.` | 28 | `house.` |
| 12 | `hey do tab.` | 29 | `down.` |
| 13 | `hey stab.` | 30 | `happy.` |
| 14 | `youtab.` | 31 | `stop.` |
| 15 | `hey there.` | 32 | `yes.` |
| 16 | `youtab is running.` | 33 | `left.` |
| 17 | `hey you had.` | 34 | `no.` |

> این جدول باید با `speaker_recording_spec.NEAR_PHRASE_ITEMS` یکی بماند؛ منبعِ حقیقت آن فایل است.

## بخش ۴ — صحبت آزاد بدون عبارت اصلی (۱ فایل، ۲ تا ۳ دقیقه)

یک فایل پیوسته در `negative_freespeech/`. حرف عادی و روزمره. **نه** عبارت اصلی و
**نه** هیچ‌کدام از عبارت‌های near را نگو.

## بخش ۵ — فقط صدای محیط بدون حرف زدن (۱ فایل، ۱ تا ۲ دقیقه)

یک فایل پیوسته در `background_only/`. صدای همان اتاق، بدون اینکه کسی حرف بزند.

---

## جمع‌بندی

| بخش | تعداد فایل |
|---|---|
| ۱ — نزدیک، پنج ادا | ۱۰ |
| ۲ — فاصله و نویز | ۹ |
| ۳ — عبارت‌های نزدیک (۳۴ ردیف) | ۳۴ |
| ۴ — صحبت آزاد | ۱ |
| ۵ — صدای محیط | ۱ |
| **کل** | **۵۵ فایل، حدود ۱۲ تا ۱۶ دقیقه** |

## ساختار پوشه (برنامه خودکار می‌سازد)

```
G:\Youtab-Wakeword-Human\incoming\E001\
  originals\
    positive_normal\   hey-youtab_normal_001.wav  ...002
    positive_slow\     hey-youtab_slow_001.wav    ...
    positive_fast\     ...
    positive_quiet\    ...
    positive_loud\     ...
    positive_farfield\ hey-youtab_farfield_001.wav ...003
    positive_noise_tv\      hey-youtab_noise-tv_001.wav ...     (E001)
    positive_noise_kitchen\ ...                                 (E001)
    near_phrase\       <slug>_001.wav               (۳۴ ردیف)
    negative_freespeech\ freespeech_001.wav
    background_only\   background_001.wav
  RECORDING_METADATA.json
  CONSENT.pdf         (هماهنگ‌کننده اضافه می‌کند)
  SHA256SUMS          (برنامه می‌سازد)
```

(برای E002 همین ساختار، با `positive_noise_street` و `positive_noise_fan`.)

## بعد از ضبط

1. یک‌بار روی خروجی، **validator فشرده** را اجرا کن (دکمهٔ Validate در برنامه): باید سبز شود.
2. سپس ingestion را با `import_speaker.py --dry-run` امتحان کن (فقط بررسی، چیزی نمی‌نویسد).
3. اصل ضبط‌ها هرگز تغییر نمی‌کنند؛ ضبطِ سختِ پذیرفته‌شده هرگز خودکار پاک نمی‌شود.

> رضایت‌نامه‌ها جدا از صدا، در `G:\Youtab-Wakeword-Consent\private` نگه‌داری می‌شوند و هرگز وارد git نمی‌شوند.
