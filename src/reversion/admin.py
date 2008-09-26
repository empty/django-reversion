"""Admin extensions for Reversion."""


from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.forms.models import model_to_dict
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext
from django.utils.encoding import force_unicode
from django.utils.html import mark_safe
from django.utils.text import capfirst
from django.utils.translation import ugettext as _

from reversion import revision
from reversion.models import Version


def deserialized_model_to_dict(deserialized_model, revision_data):
    """Converts a deserialized model to a dictionary."""
    result = model_to_dict(deserialized_model.object)
    result.update(deserialized_model.m2m_data)
    # Add parent data.
    parents = []
    model = deserialized_model.object
    for parent_class, field in model._meta.parents.items():
        attname = field.attname
        attvalue = getattr(model, attname)
        pk_name = parent_class._meta.pk.name
        for deserialized_model in revision_data:
            parent = deserialized_model.object
            if parent_class == parent.__class__ and unicode(getattr(parent, pk_name)) == unicode(getattr(model, attname)):
                parents.append(deserialized_model)
    for parent in parents:
        result.update(deserialized_model_to_dict(parent, revision_data))
    return result


class VersionAdmin(admin.ModelAdmin):
    
    """Abstract admin class for handling version controlled models."""

    revision_form_template = "reversion/revision_form.html"
    object_history_template = "reversion/object_history.html"
    
    def __call__(self, request, url):
        """Adds additional functionality to the admin class."""
        path = url or ""
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[1] == "history":
            object_id = parts[0]
            revision_id = parts[2]
            return self.revision_view(request, object_id, revision_id)
        else:
            return super(VersionAdmin, self).__call__(request, url)
    
    # TODO fieldset ordering needs to be sorted.
    # TODO inlines only recover if their specific revision is recalled.
    @transaction.commit_on_success
    @revision.create_revision
    def revision_view(self, request, object_id, log_entry_id):
        """Displays the contents of the given revision."""
        model = self.model
        content_type = ContentType.objects.get_for_model(model)
        opts = model._meta
        app_label = opts.app_label
        obj = get_object_or_404(self.model, pk=object_id)
        log_entry = get_object_or_404(LogEntry, pk=log_entry_id)
        try:
            version = Version.objects.filter(object_id=object_id,
                                             content_type=content_type,
                                             date_created__gte=log_entry.action_time).order_by("date_created")[0]
        except IndexError:
            return HttpResponseRedirect("%s%s/%s/%s/" % (self.admin_site.root_path, app_label, model.__name__.lower(), object_id))
        object_version = version.object_version
        ordered_objects = opts.get_ordered_objects()
        # Generate the form.
        revision = [version.object_version for version in version.get_revision()]
        ModelForm = self.get_form(request, obj)
        formsets = []
        form = ModelForm(instance=obj, initial=deserialized_model_to_dict(object_version, revision))
        for FormSet in self.get_formsets(request, obj):
            formset = FormSet(instance=obj)
            attname = FormSet.fk.attname
            pk_name = FormSet.model._meta.pk.name
            initial_overrides = dict(((getattr(version.object, pk_name), version) for version in revision if version.object.__class__ == FormSet.model and unicode(getattr(version.object, attname)) == object_id))
            initial = formset.initial
            for initial_row in initial:
                pk = initial_row[pk_name]
                if pk in initial_overrides:
                     initial_row.update(deserialized_model_to_dict(initial_overrides[pk], revision))
                     del initial_overrides[pk]
            initial.extend(initial_overrides.values())
            # HACK: no way to specify initial values.
            formset._total_form_count = len(initial)
            formset.initial = initial
            formset._construct_forms()
            formsets.append(formset)
        # Generate the context.
        adminForm = admin.helpers.AdminForm(form, self.get_fieldsets(request, obj), self.prepopulated_fields)
        media = self.media + adminForm.media
        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request, obj))
            inline_admin_formset = admin.helpers.InlineAdminFormSet(inline, formset, fieldsets)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media
        context = {"title": _("Revert %s") % force_unicode(opts.verbose_name),
                   "adminform": adminForm,
                   "object_id": object_id,
                   "original": obj,
                   "is_popup": False,
                   "media": mark_safe(media),
                   "inline_admin_formsets": inline_admin_formsets,
                   "errors": admin.helpers.AdminErrorList(form, formsets),
                   "root_path": self.admin_site.root_path,
                   "app_label": app_label,
                   "add": False,
                   "change": True,
                   "has_add_permission": self.has_add_permission(request),
                   "has_change_permission": self.has_change_permission(request, obj),
                   "has_delete_permission": self.has_delete_permission(request, obj),
                   "has_file_field": True, # FIXME - this should check if form or formsets have a FileField,
                   "has_absolute_url": hasattr(self.model, "get_absolute_url"),
                   "ordered_objects": ordered_objects,
                   "form_url": mark_safe(request.path),
                   "opts": opts,
                   "content_type_id": ContentType.objects.get_for_model(self.model).id,
                   "save_as": self.save_as,
                   "save_on_top": self.save_on_top,
                   "root_path": self.admin_site.root_path,}
        return render_to_response(self.revision_form_template, context, RequestContext(request))
    
    # Wrap the data-modifying views in revisions.
    add_view = revision.create_revision(admin.ModelAdmin.add_view)
    change_view = revision.create_revision(admin.ModelAdmin.change_view)
    delete_view = revision.create_revision(admin.ModelAdmin.delete_view)
    
    