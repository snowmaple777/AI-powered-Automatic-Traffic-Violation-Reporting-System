package tw.nuk.autoform

enum class Step(val action: String, val label: String) {
    Start("start", "同意聲明"), EarlyVerify("", "填表前人工驗證"), Fill("fill", "個資與違規資料"),
    Cascade("cascade", "違規大類與小類"), Details("details", "地點與敘述"),
    Upload("upload", "準備上傳附件"), Receipt("receipt", "確認附件已上傳"),
    Verify("", "官網驗證與送出"), Pending("", "等待網站回覆"), Done("", "已離開填表頁")
}

/** All checkpoints belong to one case. A document reload never consumes an attachment. */
class Workflow(val timing: String, mediaCount: Int, maximum: Int = 3) {
    val limit = minOf(maximum, mediaCount)
    var index = 0
        private set
    var baseline = -1
        private set
    var step = Step.Start
        private set

    fun arrived(page: String) {
        when {
            step == Step.Pending && page == "done" -> step = Step.Done
            step == Step.Pending && page == "fill" -> step = Step.Verify
            step == Step.Start && page == "verified" -> step = Step.Fill
            step == Step.Start && page in setOf("fill", "verify") -> {
                step = if (timing in setOf("before_fill", "pre_verification") || page == "verify") Step.EarlyVerify else Step.Fill
            }
            step == Step.EarlyVerify && page in setOf("verified", "fill") -> step = Step.Fill
        }
    }

    fun next() {
        step = when (step) {
            Step.Fill -> Step.Cascade
            Step.Cascade -> Step.Details
            Step.Details -> if (index < limit) Step.Upload else Step.Verify
            else -> step
        }
    }

    fun beginUpload(currentCount: Int): Boolean {
        if (step != Step.Upload || currentCount < 0 || index >= limit) return false
        baseline = currentCount
        step = Step.Receipt
        return true
    }

    fun receipt(count: Int): Boolean {
        if (step != Step.Receipt || count != baseline + 1) return false
        index++
        step = if (index < limit) Step.Upload else Step.Verify
        return true
    }

    fun retryUnsentUpload() {
        if (step == Step.Receipt) step = Step.Upload
    }

    fun fileAttached() {
        if (step != Step.Upload || index >= limit) return
        index++
        if (index == limit) step = Step.Verify
    }

    // A browser reload discards native file inputs. Repopulate them on the replacement form.
    fun formReloaded() {
        if (step == Step.Verify || step == Step.Pending) {
            // A validation response can redisplay the user's corrected form with empty file inputs.
            index = 0
            step = if (limit > 0) Step.Upload else Step.Verify
        } else if (step in setOf(Step.Fill, Step.Cascade, Step.Details, Step.Upload)) {
            index = 0
            step = Step.Fill
        }
    }

    fun siteSubmitObserved() {
        if (step == Step.Verify) step = Step.Pending
    }
}
