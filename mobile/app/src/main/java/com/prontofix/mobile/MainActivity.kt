package com.prontofix.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Button
import android.widget.Toast
import androidx.activity.ComponentActivity

class MainActivity : ComponentActivity() {
    private lateinit var webView: WebView
    private lateinit var addressButton: Button
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

    private val preferences by lazy { getSharedPreferences("prontofix", MODE_PRIVATE) }
    private val defaultServer = "https://truenas-scale.tail522166.ts.net/"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        addressButton = Button(this).apply {
            text = "Адрес сервера"
            setOnClickListener { showServerDialog() }
        }
        root.addView(addressButton, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ))

        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = false
        webView.settings.allowContentAccess = true
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                // Keep navigation inside the configured server; open external sites in the browser.
                val base = Uri.parse(serverUrl())
                return if (uri.host == base.host && uri.scheme == base.scheme) {
                    false
                } else {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                    true
                }
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                this@MainActivity.fileChooserCallback?.onReceiveValue(null)
                this@MainActivity.fileChooserCallback = filePathCallback
                return try {
                    val intent = fileChooserParams?.createIntent()
                        ?: Intent(Intent.ACTION_GET_CONTENT).apply { type = "*/*" }
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST)
                    true
                } catch (_: Exception) {
                    this@MainActivity.fileChooserCallback = null
                    Toast.makeText(this@MainActivity, "Не удалось открыть выбор файла", Toast.LENGTH_LONG).show()
                    false
                }
            }
        }
        root.addView(webView, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
        ))
        setContentView(root)

        if (savedInstanceState == null) {
            webView.loadUrl(serverUrl())
        } else {
            webView.restoreState(savedInstanceState)
        }
    }

    private fun serverUrl(): String {
        val raw = preferences.getString("server_url", defaultServer) ?: defaultServer
        return if (raw.endsWith("/")) raw else "$raw/"
    }

    private fun showServerDialog() {
        val input = EditText(this).apply {
            setSingleLine(true)
            hint = "https://server.example/"
            setText(serverUrl())
        }
        AlertDialog.Builder(this)
            .setTitle("Адрес сервера ProntoFix")
            .setMessage("Укажите HTTPS-адрес сервера. Для удалённого доступа сначала подключите Tailscale.")
            .setView(input)
            .setNegativeButton("Отмена", null)
            .setNeutralButton("По умолчанию") { _, _ ->
                preferences.edit().remove("server_url").apply()
                webView.loadUrl(defaultServer)
            }
            .setPositiveButton("Сохранить") { _, _ ->
                val value = input.text.toString().trim()
                val uri = Uri.parse(value)
                if (uri.scheme != "https" || uri.host.isNullOrBlank()) {
                    Toast.makeText(this, "Введите корректный HTTPS-адрес", Toast.LENGTH_LONG).show()
                } else {
                    preferences.edit().putString("server_url", value).apply()
                    webView.loadUrl(serverUrl())
                }
            }
            .show()
    }

    @Deprecated("Deprecated in Android, retained for compatibility with file chooser callback")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == FILE_CHOOSER_REQUEST) {
            val results = if (resultCode == Activity.RESULT_OK && data?.data != null) {
                arrayOf(data.data!!)
            } else {
                WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            }
            fileChooserCallback?.onReceiveValue(results)
            fileChooserCallback = null
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        webView.saveState(outState)
        super.onSaveInstanceState(outState)
    }

    @Deprecated("Deprecated in Android, retained for compatibility")
    override fun onBackPressed() {
        if (::webView.isInitialized && webView.canGoBack()) webView.goBack()
        else super.onBackPressed()
    }

    override fun onDestroy() {
        fileChooserCallback?.onReceiveValue(null)
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        private const val FILE_CHOOSER_REQUEST = 4107
    }
}
