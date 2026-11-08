# Video Downloader Telegram Bot

بوت Telegram لتنزيل وإرسال الفيديوهات من الروابط العامة التي تدعمها مكتبة yt-dlp.

## منصات محتملة الدعم
YouTube وYouTube Shorts وTikTok وInstagram وFacebook وX/Twitter وReddit وVimeo وDailymotion وغيرها من المواقع المدعومة في yt-dlp. الدعم الفعلي يختلف حسب المنصة والرابط وقد يتغير عند تحديث المواقع.

## النشر على Railway
1. اربط هذا المستودع بخدمة Railway على فرع `main`.
2. تأكد أن البناء يستخدم ملف `Dockerfile`.
3. أضف متغير البيئة `BOT_TOKEN` بقيمة توكن BotFather، ولا تضعه في GitHub.
4. انتظر نجاح أحدث Deployment.
5. افتح البوت على Telegram وأرسل `/start`.

## متطلبات التشغيل
- Python 3.12
- FFmpeg لدمج الصوت والفيديو عند الحاجة.
- Deno وyt-dlp EJS للمواقع التي تتطلب JavaScript challenge solving.
- يعمل البوت كـ Worker ويستخدم Telegram long polling؛ لا يحتاج إلى Domain أو Public HTTP port.

## الحدود
- الحد الأقصى للإرسال 49MB والمدة 20 دقيقة.
- الروابط العامة فقط؛ لا يدعم المحتوى الخاص أو المدفوع أو المحمي بـ DRM أو الذي يتطلب تجاوز تسجيل الدخول/قيود الوصول.
- قد تمنع بعض المنصات التنزيل أو تتطلب تغييرات مستقبلية في yt-dlp.
- استخدمه فقط لتنزيل محتوى تملكه أو لديك إذن بتنزيله.
