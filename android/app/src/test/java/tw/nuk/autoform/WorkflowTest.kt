package tw.nuk.autoform

import org.junit.Assert.*
import org.junit.Test

class WorkflowTest {
    private fun filling(count: Int): Workflow = Workflow("before_submit", count).apply {
        arrived("fill")
        repeat(3) { next() }
    }
    @Test fun uploadNeedsNewServerCountAndStopsAtThree() {
        val flow = filling(5)
        repeat(3) { index ->
            assertTrue(flow.beginUpload(index))
            flow.arrived("fill")
            assertFalse(flow.receipt(index))
            assertFalse(flow.receipt(index + 2))
            assertEquals(index, flow.index)
            assertTrue(flow.receipt(index + 1))
            assertFalse(flow.receipt(index + 1))
        }
        assertEquals(Step.Verify, flow.step)
    }
    @Test fun onlyAnObservedWebsiteSubmissionStartsWaitingForAResult() {
        val flow = filling(0)
        flow.arrived("done")
        assertEquals(Step.Verify, flow.step)
        flow.siteSubmitObserved()
        assertEquals(Step.Pending, flow.step)
        flow.arrived("fill")
        assertEquals(Step.Verify, flow.step)
        flow.siteSubmitObserved()
        flow.arrived("unknown")
        assertEquals(Step.Pending, flow.step)
        flow.arrived("done")
        assertEquals(Step.Done, flow.step)
    }
    @Test fun earlyVerificationPausesBeforeReporterFields() {
        for (timing in listOf("before_fill","pre_verification")) {
            val flow = Workflow(timing, 0)
            flow.arrived("fill")
            assertEquals(Step.EarlyVerify, flow.step)
            flow.arrived("verified")
            assertEquals(Step.Fill, flow.step)
        }
    }

    @Test fun authenticatedSessionCanStartAtUnlockedCaseForm() {
        val flow = Workflow("pre_verification", 0)
        flow.arrived("verified")
        assertEquals(Step.Fill,flow.step)
    }

    @Test fun validationPostbackRestoresNativeFileSlotsAndPreservesManualEdits() {
        val flow = Workflow("before_submit",5,5)
        flow.arrived("verified")
        repeat(3) { flow.next() }
        repeat(5) { flow.fileAttached() }
        assertEquals(Step.Verify,flow.step)
        flow.siteSubmitObserved()
        flow.formReloaded()
        flow.arrived("verified")
        assertEquals(Step.Upload,flow.step)
        assertEquals(0,flow.index)
    }
}
