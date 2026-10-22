from collections import OrderedDict

from odoo import models

TAG_REPORT = 'di_tag_export.report_product_tag'


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _render_qweb_pdf_prepare_streams(self, report_ref, data, res_ids=None):
        """Render a large cartellino print in several wkhtmltopdf runs.

        wkhtmltopdf keeps every page of a run in memory and inherits the Odoo
        worker's memory cap, so a whole arrival of several thousand labels in
        one run was killed (error -11, "Memory limit too low"). Each run here
        gets ``TAGS_PER_DOCUMENT`` labels, passed as ``tag_lines`` so the
        report prints exactly that slice in that order. ``_render_qweb_pdf``
        already joins every stream returned here into one PDF, so the worker
        still downloads a single file, in the same order as before.

        Anything that fits in one run, and every other report, goes through
        untouched.
        """
        report = self._get_report(report_ref)
        if report.report_name != TAG_REPORT:
            return super()._render_qweb_pdf_prepare_streams(
                report_ref, data, res_ids=res_ids)

        batches = self.env['report.' + TAG_REPORT]._tag_line_batches(
            res_ids, data)
        if len(batches) <= 1:
            return super()._render_qweb_pdf_prepare_streams(
                report_ref, data, res_ids=res_ids)

        streams = OrderedDict()
        for number, lines in enumerate(batches):
            batch_data = dict(data or {}, tag_lines=lines)
            batch_data.pop('tag_quantities', None)
            # No ids: with them Odoo would try to split the PDF back into one
            # file per product, which a label print has no use for.
            rendered = super()._render_qweb_pdf_prepare_streams(
                report_ref, batch_data, res_ids=None)
            for index, stream in enumerate(rendered.values()):
                streams[f'di_tag_batch_{number}_{index}'] = stream
        return streams
