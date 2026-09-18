package com.zkimport.app

/**
 * حالة على مستوى العملية (Process): تمنع تشغيل خادم بايثون مرتين على نفس
 * المنفذ لو أعاد أندرويد إنشاء الشاشة دون قتل العملية.
 */
object PythonServerState {
    @Volatile
    var started = false
}
