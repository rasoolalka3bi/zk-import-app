package com.zkimport.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.animation.AlphaAnimation
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var loadingOverlay: View

    /** رد اختيار الملف المعلّق من الواجهة (input type="file") - بدون هذا
     * لا يفتح WebView أي نافذة لاختيار الملفات إطلاقًا. */
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooserLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val callback = fileChooserCallback ?: return@registerForActivityResult
            fileChooserCallback = null
            callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        loadingOverlay = findViewById(R.id.loadingOverlay)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String?) {
                super.onPageFinished(view, url)
                hideLoadingOverlay()
            }
        }
        webView.addJavascriptInterface(AndroidBridge(), "AndroidBridge")

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                // إلغاء أي طلب سابق لم يكتمل (وإلا يتوقف زر اختيار الملف عن العمل)
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = callback
                return try {
                    // نوع عام */* عمدًا: بعض الهواتف لا تتعرّف على json فتُخفيه
                    // من القائمة - والواجهة تتحقق من صلاحية الملف بنفسها
                    val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = "*/*"
                    }
                    fileChooserLauncher.launch(Intent.createChooser(intent, "اختر الملف"))
                    true
                } catch (e: Exception) {
                    fileChooserCallback = null
                    Toast.makeText(this@MainActivity, "تعذّر فتح اختيار الملفات", Toast.LENGTH_SHORT).show()
                    false
                }
            }
        }

        startPythonServer()

        Handler(Looper.getMainLooper()).postDelayed({
            webView.loadUrl("http://127.0.0.1:5001/")
        }, 2000)
    }

    /** يُخفي شاشة التحميل بتلاشٍ ناعم بمجرد جاهزية الواجهة فعليًا. */
    private fun hideLoadingOverlay() {
        if (loadingOverlay.visibility != View.VISIBLE) return
        val fadeOut = AlphaAnimation(1f, 0f).apply { duration = 350 }
        loadingOverlay.startAnimation(fadeOut)
        loadingOverlay.visibility = View.GONE
    }

    private fun startPythonServer() {
        if (PythonServerState.started) return
        PythonServerState.started = true

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        Thread {
            try {
                val py = Python.getInstance()
                val appModule = py.getModule("app")
                appModule.callAttr("start", filesDir.absolutePath)
            } catch (e: PyException) {
                runOnUiThread {
                    Toast.makeText(this, "خطأ في تشغيل التطبيق: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    inner class AndroidBridge {

        /** يفتح محادثة واتساب مع المسؤول والنص جاهز في خانة الكتابة. واتساب
         * لا تسمح لأي تطبيق بإرسال الرسالة تلقائيًا، فتبقى ضغطة الإرسال على
         * المستخدم. وإن لم يكن واتساب مثبّتًا، نَنسخ النص بدل ترك الزر بلا فائدة. */
        @JavascriptInterface
        fun openWhatsApp(url: String, text: String) {
            runOnUiThread {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                } catch (e: Exception) {
                    val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("نتيجة إضافة الموظفين", text))
                    Toast.makeText(this@MainActivity,
                        "واتساب غير مثبّت — نُسخت النتيجة، الصقها وأرسلها للمسؤول",
                        Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
