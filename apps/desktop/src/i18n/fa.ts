import { defineLocale } from './define-locale'

// Technical identifiers and untranslated specialist messages retain English.
export const fa = defineLocale({
  runtimePlugins: {
    title: "افزونه‌های Runtime",
    description: "یکپارچه‌سازی‌های همراه برنامه و نصب‌شدهٔ ایجنت. دسترسی به هر افزونه به وابستگی‌ها و تنظیمات حساب آن بستگی دارد.",
    empty: "افزونهٔ Runtime یافت نشد.",
    failed: "بارگذاری افزونه‌های Runtime ناموفق بود.",
  },
  common: {
    apply: 'اعمال', back: 'بازگشت', save: 'ذخیره', saving: 'در حال ذخیره…', cancel: 'لغو',
    change: 'تغییر', choose: 'انتخاب', clear: 'پاک کردن', close: 'بستن', collapse: 'جمع کردن',
    confirm: 'تأیید', connect: 'اتصال', connecting: 'در حال اتصال', continue: 'ادامه',
    copied: 'کپی شد', copy: 'کپی', copyFailed: 'کپی ناموفق بود', delete: 'حذف', docs: 'راهنما',
    done: 'انجام شد', error: 'خطا', expand: 'باز کردن', failed: 'ناموفق', formatJson: 'مرتب کردن JSON',
    free: 'رایگان', loading: 'در حال بارگذاری…', notSet: 'تنظیم نشده', refresh: 'تازه‌سازی',
    remove: 'حذف', replace: 'جایگزینی', retry: 'تلاش دوباره', run: 'اجرا', send: 'ارسال',
    set: 'تنظیم', skip: 'رد کردن', update: 'به‌روزرسانی', tryHint: term => `«${term}» را امتحان کنید`,
    on: 'روشن', off: 'خاموش'
  },
  language: {
    label: 'زبان', description: 'زبان رابط برنامه را انتخاب کنید.', saving: 'در حال ذخیره زبان…',
    saveError: 'ذخیره زبان ناموفق بود.', switchTo: 'تغییر زبان', searchPlaceholder: 'جست‌وجوی زبان…',
    noResults: 'زبانی پیدا نشد'
  },
  settings: {
    about: {
      heading: 'Youtab Desktop', version: value => `نسخه ${value}`, versionUnavailable: 'نسخه در دسترس نیست',
      updates: 'به‌روزرسانی‌ها', checkNow: 'بررسی اکنون', checking: 'در حال بررسی…', seeWhatsNew: 'تغییرات جدید',
      updateNow: 'به‌روزرسانی اکنون', releaseNotes: 'یادداشت انتشار', onLatest: 'نسخه شما به‌روز است.',
      installing: 'به‌روزرسانی در حال نصب است.', cantUpdate: 'این نسخه از داخل برنامه به‌روز نمی‌شود.',
      cantReach: 'اتصال به سرور به‌روزرسانی ناموفق بود.', tapCheck: 'برای بررسی، «بررسی اکنون» را بزنید.',
      updateReady: count => `به‌روزرسانی جدید با ${count} تغییر آماده است.`, lastChecked: age => `آخرین بررسی: ${age}`,
      justNowSuffix: ' · همین حالا', automaticUpdates: 'به‌روزرسانی خودکار',
      automaticUpdatesDesc: 'Youtab در پس‌زمینه به‌روزرسانی‌ها را بررسی می‌کند و آماده شدن آن‌ها را اطلاع می‌دهد.',
      branchCommit: (branch, commit) => `شاخه ${branch} · Commit ${commit}`, never: 'هرگز', justNow: 'همین حالا',
      minAgo: count => `${count} دقیقه پیش`, hoursAgo: count => `${count} ساعت پیش`, daysAgo: count => `${count} روز پیش`
    },
    closeSettings: 'بستن تنظیمات', exportConfig: 'خروجی تنظیمات', importConfig: 'ورود تنظیمات',
    resetToDefaults: 'بازگردانی تنظیمات پیش‌فرض', resetConfirm: 'همه تنظیمات به حالت پیش‌فرض بازگردند؟',
    nav: {
      providers: 'ارائه‌دهندگان', providerAccounts: 'حساب‌ها', providerApiKeys: 'کلیدهای API',
      providerCustomEndpoints: 'نشانی‌های سفارشی', gateway: 'Gateway', apiKeys: 'ابزارها و کلیدها',
      keybinds: 'میان‌برهای صفحه‌کلید', keysTools: 'ابزارها', keysSettings: 'تنظیمات',
      archivedChats: 'گفت‌وگوهای بایگانی', about: 'درباره', billing: 'صورتحساب', notifications: 'اعلان‌ها', plugins: 'افزونه‌ها'
    },
    sections: {
      model: 'مدل', chat: 'گفت‌وگو', appearance: 'ظاهر', workspace: 'محیط کار', safety: 'ایمنی',
      memory: 'حافظه و زمینه', voice: 'صدا', advanced: 'پیشرفته'
    },
    modeOptions: {
      light: { label: 'روشن', description: 'رابط روشن' },
      dark: { label: 'تیره', description: 'محیط کار با نور کمتر' },
      system: { label: 'سیستم', description: 'پیروی از ظاهر سیستم' }
    },
    appearance: {
      title: 'ظاهر', intro: 'حالت، روشنایی را تعیین می‌کند و پوسته، رنگ‌ها و ظاهر گفت‌وگو را.',
      themeTitle: 'پوسته', themeDesc: 'رنگ‌بندی رابط برنامه؛ حالت انتخاب‌شده روی آن اعمال می‌شود.',
      colorMode: 'حالت رنگ', colorModeDesc: 'یک حالت انتخاب کنید یا از تنظیم سیستم پیروی کنید.',
      toolViewTitle: 'نمایش اجرای ابزار', toolViewDesc: 'نمای فنی، ورودی و خروجی کامل را نشان می‌دهد.',
      uiScaleTitle: 'اندازه رابط', uiScaleDesc: percent => `اندازه متن و کنترل‌ها در برنامه؛ فعلی: ${percent}٪.`,
      translucencyTitle: 'شفافیت پنجره', translucencyDesc: 'نمایش پس‌زمینه دسکتاپ از پشت پنجره.',
      backdropTitle: 'پس‌زمینه گفت‌وگو', backdropDesc: 'تصویر کم‌رنگ پشت گفت‌وگو.',
      reactionsTitle: 'واکنش به پیام', reactionsDesc: 'با ایموجی به پیام‌ها واکنش نشان دهید.'
    }
  },
  sidebar: {
    nav: { 'new-session': 'گفت‌وگوی جدید', skills: 'اتصال‌ها', messaging: 'پیام‌رسانی', artifacts: 'خروجی‌ها' },
    searchAria: 'جست‌وجوی گفت‌وگو', searchPlaceholder: 'جست‌وجوی گفت‌وگو…', clearSearch: 'پاک کردن جست‌وجو',
    pinned: 'سنجاق‌شده', sessions: 'گفت‌وگوها', results: 'نتایج', cronJobs: 'کارهای زمان‌بندی‌شده'
  },
  notifications: {
    native: { approvalTitle: 'نیاز به تأیید شما', approveAction: 'تأیید', rejectAction: 'رد',
      inputTitle: 'نیاز به پاسخ شما', inputBody: 'Youtab منتظر پاسخ شما است.' }
  },
  shell: {
    approvalMode: { title: 'حالت تأیید', manual: 'دستی', manualDescription: 'پیش از اقدامات نیازمند تأیید، اجازه بپرس.',
      smart: 'هوشمند', off: 'خاموش', offDescription: 'اجرا بدون درخواست تأیید' }
  },
  assistant: {
    clarify: { notReady: 'پرسش هنوز آماده نیست', gatewayDisconnected: 'Gateway متصل نیست',
      sendFailed: 'ارسال پاسخ ناموفق بود', loadingQuestion: 'در حال دریافت پرسش…',
      other: 'گزینه دیگر (پاسخ خود را بنویسید)', placeholder: 'پاسخ خود را بنویسید…',
      skip: 'رد کردن', skipped: 'رد شد', continueLabel: 'ادامه',
      lateAnswer: (question, choice) => `پاسخ من به «${question}»: ${choice}`,
      lateAnswerTip: 'ارسال این پاسخ به‌عنوان پیام بعدی', lateAnswerHint: 'این پرسش دیگر منتظر پاسخ نیست؛ پاسخ را به‌عنوان پیام بعدی آماده کنید.' },
    approval: { run: 'اجرا', reject: 'رد', command: 'دستور', moreOptions: 'گزینه‌های بیشتر',
      allowSession: 'اجازه برای این گفت‌وگو', alwaysAllow: 'همیشه اجازه بده', alwaysAllowMenu: 'همیشه اجازه بده…' }
  }
})
