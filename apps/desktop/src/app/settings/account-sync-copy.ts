import type { Locale } from '@/i18n'

const en = {
  discoveryLimited: 'History is still being scanned. Existing shared chats continue syncing.',
  incomplete: 'Some chats have not synced. Their local originals are preserved; sync is incomplete until these history or export limits are resolved.',
  savePreferences: 'Save language and appearance to account', applyPreferences: 'Apply account language and appearance',
  continueChat: 'Continue on this device',
  loading: 'Loading account sync…', retry: 'Try again',
  title: 'Account sync', consent: 'Enable sync for all chats', cancel: 'Cancel',
  description: 'All existing and new chat text is added to your Youtab account with its original dates and becomes available on your other devices. Local history is preserved. Local files, tool results and API keys are excluded.',
  unavailable: 'Account sync is unavailable. Sign in to Youtab, or try again when the service is available.',
  error: 'Sync did not finish. Pending changes are kept on this device. Try again.',
  sync: 'Sync now', local: 'Choose a local chat to share', share: 'Share chat',
  cloud: 'Chats in your account', empty: 'No shared chats yet.', remove: 'Delete from account',
  confirm: 'Delete this shared chat from your account and other synced devices? Local chat copies remain.',
  pending: 'Changes waiting to sync', channel: 'Update channel',
  channelDescription: 'Stable receives tested releases. Pilot receives releases for testing before users.',
  channelError: 'Could not change the update channel. Try again.'
}

const fa: typeof en = {
  discoveryLimited: 'بررسی تاریخچه هنوز کامل نشده است. چت‌های مشترک همچنان همگام می‌شوند.',
  incomplete: 'برخی چت‌ها هنوز همگام نشده‌اند. نسخهٔ اصلی محلی محفوظ است؛ تا رفع محدودیت تاریخچه یا خطای دریافت، انتقال کامل نیست.',
  savePreferences: 'ذخیرهٔ زبان و ظاهر در حساب', applyPreferences: 'اعمال زبان و ظاهر حساب',
  continueChat: 'ادامهٔ چت روی این دستگاه',
  loading: 'در حال دریافت وضعیت همگام‌سازی…', retry: 'تلاش دوباره',
  title: 'همگام‌سازی حساب', consent: 'همگام‌سازی همهٔ چت‌ها', cancel: 'انصراف',
  description: 'متن همهٔ چت‌های قدیمی و جدید با زمان اصلی به حساب Youtab اضافه می‌شود و روی دستگاه‌های دیگر در دسترس است. تاریخچهٔ محلی حفظ می‌شود. فایل‌های محلی، خروجی ابزارها و کلیدهای API ارسال نمی‌شوند.',
  unavailable: 'همگام‌سازی حساب در دسترس نیست. وارد Youtab شوید یا پس از آماده‌شدن سرویس دوباره تلاش کنید.',
  error: 'همگام‌سازی کامل نشد. تغییرات ارسال‌نشده روی این دستگاه حفظ شده‌اند. دوباره تلاش کنید.',
  sync: 'همگام‌سازی اکنون', local: 'انتخاب چت محلی برای اشتراک', share: 'اشتراک چت',
  cloud: 'چت‌های حساب شما', empty: 'هنوز چتی به اشتراک گذاشته نشده است.', remove: 'حذف از حساب',
  confirm: 'این چت از حساب و دستگاه‌های همگام‌شده حذف شود؟ نسخه‌های محلی چت حفظ می‌شوند.',
  pending: 'تغییرات منتظر ارسال', channel: 'کانال به‌روزرسانی',
  channelDescription: 'Stable نسخه‌های آزمایش‌شده را دریافت می‌کند. Pilot نسخه‌ها را برای آزمایش پیش از کاربران دریافت می‌کند.',
  channelError: 'تغییر کانال انجام نشد. دوباره تلاش کنید.'
}

const nl: typeof en = {
  discoveryLimited: 'De geschiedenis wordt nog gescand. Gedeelde chats blijven synchroniseren.',
  incomplete: 'Sommige chats zijn nog niet gesynchroniseerd. Lokale originelen blijven behouden; de overdracht is onvolledig totdat de limieten of exportfouten zijn opgelost.',
  savePreferences: 'Taal en weergave opslaan in account', applyPreferences: 'Taal en weergave van account toepassen',
  continueChat: 'Doorgaan op dit apparaat',
  loading: 'Accountsynchronisatie laden…', retry: 'Opnieuw proberen',
  title: 'Accountsynchronisatie', consent: 'Alle chats synchroniseren', cancel: 'Annuleren',
  description: 'Bestaande en nieuwe chattekst wordt met de oorspronkelijke datums aan je Youtab-account toegevoegd en is beschikbaar op andere apparaten. Lokale geschiedenis blijft behouden. Lokale bestanden, toolresultaten en API-sleutels worden uitgesloten.',
  unavailable: 'Accountsynchronisatie is niet beschikbaar. Meld je aan bij Youtab of probeer het opnieuw zodra de dienst beschikbaar is.',
  error: 'Synchronisatie is niet voltooid. Wachtende wijzigingen blijven op dit apparaat bewaard. Probeer opnieuw.',
  sync: 'Nu synchroniseren', local: 'Selecteer een lokale chat om te delen', share: 'Chat delen',
  cloud: 'Chats in je account', empty: 'Nog geen gedeelde chats.', remove: 'Verwijderen uit account',
  confirm: 'Deze gedeelde chat verwijderen uit je account en gesynchroniseerde apparaten? Lokale kopieën blijven behouden.',
  pending: 'Wijzigingen die wachten op synchronisatie', channel: 'Updatekanaal',
  channelDescription: 'Stable ontvangt geteste releases. Pilot ontvangt releases om te testen voordat gebruikers ze ontvangen.',
  channelError: 'Het updatekanaal kon niet worden gewijzigd. Probeer opnieuw.'
}

const zh: typeof en = {
  discoveryLimited: '仍在扫描历史记录。已共享的聊天继续同步。',
  incomplete: '部分聊天尚未同步。本地原始记录已保留；解决历史限制或导出错误之前，迁移仍不完整。',
  savePreferences: '将语言和外观保存到账户', applyPreferences: '应用账户语言和外观',
  continueChat: '在此设备继续',
  loading: '正在加载账户同步…', retry: '重试',
  title: '账户同步', consent: '同步所有聊天', cancel: '取消',
  description: '所有现有和新聊天文本按原始时间添加到 Youtab 帐户，并可在其他设备上访问。本地历史记录保留。本地文件、工具结果和 API 密钥不会上传。',
  unavailable: '账户同步不可用。请登录 Youtab，或在服务可用时重试。',
  error: '同步未完成。待发送的更改仍保存在此设备上，请重试。',
  sync: '立即同步', local: '选择要共享的本地聊天', share: '共享聊天',
  cloud: '账户中的聊天', empty: '尚无共享聊天。', remove: '从账户删除',
  confirm: '从账户和同步设备删除此共享聊天？本地副本将保留。',
  pending: '等待同步的更改', channel: '更新渠道',
  channelDescription: 'Stable 接收经过测试的版本；Pilot 用于在用户收到版本前进行测试。',
  channelError: '无法更改更新渠道，请重试。'
}

const zhHant: typeof en = {
  discoveryLimited: '仍在掃描歷史記錄。已共享的聊天繼續同步。',
  incomplete: '部分聊天尚未同步。本機原始記錄已保留；解決歷史限制或匯出錯誤之前，移轉仍不完整。',
  savePreferences: '將語言和外觀儲存至帳戶', applyPreferences: '套用帳戶語言和外觀',
  continueChat: '在此裝置繼續',
  loading: '正在載入帳戶同步…', retry: '重試',
  title: '帳戶同步', consent: '同步所有聊天', cancel: '取消',
  description: '所有現有和新聊天文字依原始時間加入 Youtab 帳戶，並可在其他裝置上存取。本機歷史記錄保留。本機檔案、工具結果和 API 金鑰不會上傳。',
  unavailable: '帳戶同步無法使用。請登入 Youtab，或在服務可用時重試。',
  error: '同步未完成。待傳送的變更仍保存在此裝置上，請重試。',
  sync: '立即同步', local: '選擇要共享的本機聊天', share: '共享聊天',
  cloud: '帳戶中的聊天', empty: '尚無共享聊天。', remove: '從帳戶刪除',
  confirm: '從帳戶和同步裝置刪除此共享聊天？本機副本將保留。',
  pending: '等待同步的變更', channel: '更新通道',
  channelDescription: 'Stable 接收已測試的版本；Pilot 用於在使用者收到版本前測試。',
  channelError: '無法變更更新通道，請重試。'
}

const ja: typeof en = {
  discoveryLimited: '履歴のスキャンはまだ完了していません。共有済みチャットの同期は続きます。',
  incomplete: '一部のチャットは未同期です。ローカルの原本は保持されます。履歴制限やエクスポートの問題が解消するまで移行は未完了です。',
  savePreferences: '言語と外観をアカウントに保存', applyPreferences: 'アカウントの言語と外観を適用',
  continueChat: 'このデバイスで続ける',
  loading: 'アカウント同期を読み込み中…', retry: '再試行',
  title: 'アカウント同期', consent: 'すべてのチャットを同期', cancel: 'キャンセル',
  description: '既存と新規のチャットテキストを元の日時で Youtab アカウントに追加し、他のデバイスから利用できます。ローカル履歴は保持されます。ローカルファイル、ツール結果、API キーは送信されません。',
  unavailable: 'アカウント同期を利用できません。Youtab にログインするか、サービスが利用可能になったら再試行してください。',
  error: '同期が完了しませんでした。未送信の変更はこのデバイスに保存されています。再試行してください。',
  sync: '今すぐ同期', local: '共有するローカルチャットを選択', share: 'チャットを共有',
  cloud: 'アカウントのチャット', empty: '共有したチャットはありません。', remove: 'アカウントから削除',
  confirm: 'このチャットをアカウントと同期デバイスから削除しますか？ローカルのコピーは残ります。',
  pending: '同期待ちの変更', channel: '更新チャネル',
  channelDescription: 'Stable はテスト済みのリリースを受信します。Pilot はユーザーへの提供前のテスト用です。',
  channelError: '更新チャネルを変更できませんでした。再試行してください。'
}

const ar: typeof en = {
  discoveryLimited: 'لا يزال فحص السجل جاريًا. تستمر مزامنة المحادثات المشتركة.',
  incomplete: 'لم تتم مزامنة بعض المحادثات. النسخ الأصلية المحلية محفوظة؛ النقل غير مكتمل حتى تُحل حدود السجل أو أخطاء التصدير.',
  savePreferences: 'حفظ اللغة والمظهر في الحساب', applyPreferences: 'تطبيق لغة الحساب ومظهره',
  continueChat: 'متابعة على هذا الجهاز',
  loading: 'جارٍ تحميل مزامنة الحساب…', retry: 'حاول مجددًا',
  title: 'مزامنة الحساب', consent: 'مزامنة جميع المحادثات', cancel: 'إلغاء',
  description: 'يُضاف نص المحادثات القديمة والجديدة إلى حساب Youtab بتواريخها الأصلية ويصبح متاحًا على أجهزتك الأخرى. يُحفظ السجل المحلي. لا تُرسل الملفات المحلية أو نتائج الأدوات أو مفاتيح API.',
  unavailable: 'مزامنة الحساب غير متاحة. سجّل الدخول إلى Youtab أو حاول عندما تتاح الخدمة.',
  error: 'لم تكتمل المزامنة. التغييرات المعلقة محفوظة على هذا الجهاز. حاول مجددًا.',
  sync: 'مزامنة الآن', local: 'اختر محادثة محلية للمشاركة', share: 'مشاركة المحادثة',
  cloud: 'محادثات حسابك', empty: 'لا توجد محادثات مشتركة بعد.', remove: 'حذف من الحساب',
  confirm: 'حذف هذه المحادثة من الحساب والأجهزة المتزامنة؟ ستبقى النسخ المحلية.',
  pending: 'تغييرات تنتظر المزامنة', channel: 'قناة التحديث',
  channelDescription: 'تستقبل Stable الإصدارات المختبرة. تستقبل Pilot الإصدارات للاختبار قبل المستخدمين.',
  channelError: 'تعذر تغيير قناة التحديث. حاول مجددًا.'
}

const copies: Record<Locale, typeof en> = { en, fa, nl, zh, 'zh-hant': zhHant, ja, ar }
export const accountSyncCopy = (locale: Locale) => copies[locale]
