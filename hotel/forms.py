from django import forms
from django.contrib.auth import get_user_model

from accounts.models import Role
from accounts.permissions import PERMISSIONS
from .models import (Amenity, Coupon, Guest, Hotel, MaintenanceTicket, PricingRule, RatePlan, Review, Room, RoomBlock, RoomType, Service, TaxRule)

User = get_user_model()


class Styled:
    """Applies the portal's CSS classes and sensible widgets to every field."""

    def style(self):
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs["class"] = "chk"
            elif isinstance(w, forms.CheckboxSelectMultiple):
                pass
            else:
                w.attrs["class"] = (w.attrs.get("class", "") + " input").strip()
            if isinstance(f, forms.DateField):
                w.input_type = "date"
                w.format = "%Y-%m-%d"
            if isinstance(f, forms.TimeField):
                w.input_type = "time"
                w.format = "%H:%M"
            if isinstance(w, forms.Textarea):
                w.attrs.setdefault("rows", 3)


class SForm(Styled, forms.Form):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.style()


class SModelForm(Styled, forms.ModelForm):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.style()


class RoomTypeForm(SModelForm):
    class Meta:
        model = RoomType
        exclude = ["hotel", "slug"]
        widgets = {"amenities": forms.CheckboxSelectMultiple}


class RoomForm(SModelForm):
    class Meta:
        model = Room
        fields = ["room_number", "room_type", "floor", "status", "active", "notes"]


class RatePlanForm(SModelForm):
    class Meta:
        model = RatePlan
        fields = "__all__"


class PricingRuleForm(SModelForm):
    class Meta:
        model = PricingRule
        fields = "__all__"

    def clean(self):
        c = super().clean()
        if c.get("start_date") and c.get("end_date") and c["end_date"] < c["start_date"]:
            raise forms.ValidationError("End date must be after start date.")
        wd = c.get("weekdays", "")
        if wd and not all(x.strip().isdigit() and 0 <= int(x) <= 6 for x in wd.split(",")):
            self.add_error("weekdays", "Use numbers 0–6 separated by commas (0=Mon … 6=Sun).")
        return c


class CouponForm(SModelForm):
    class Meta:
        model = Coupon
        exclude = ["used_count"]
        widgets = {"room_types": forms.CheckboxSelectMultiple}

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class ServiceForm(SModelForm):
    class Meta:
        model = Service
        fields = "__all__"


class AmenityForm(SModelForm):
    class Meta:
        model = Amenity
        fields = "__all__"


class TaxRuleForm(SModelForm):
    class Meta:
        model = TaxRule
        exclude = ["hotel"]


class GuestForm(SModelForm):
    class Meta:
        model = Guest
        exclude = []
        widgets = {"date_of_birth": forms.DateInput()}


class BlockForm(SModelForm):
    class Meta:
        model = RoomBlock
        fields = ["room", "start_date", "end_date", "reason", "notes"]


class TicketForm(SModelForm):
    class Meta:
        model = MaintenanceTicket
        fields = ["room", "problem", "priority", "assigned_to", "notes", "blocks_room"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["assigned_to"].queryset = User.objects.filter(is_active=True)
        self.fields["room"].queryset = Room.objects.filter(active=True)


class HotelForm(SModelForm):
    class Meta:
        model = Hotel
        exclude = ["latitude", "longitude"]
        widgets = {"check_in_time": forms.TimeInput(), "check_out_time": forms.TimeInput(), "description": forms.Textarea()}


class UserForm(SModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), help_text="Leave blank to keep the current password.")

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone", "role", "is_active", "password"]

    def clean_password(self):
        pw = self.cleaned_data.get("password")
        if not pw and not self.instance.pk:
            raise forms.ValidationError("A password is required for new users.")
        if pw:
            from django.contrib.auth.password_validation import validate_password

            validate_password(pw, self.instance if self.instance.pk else None)
        return pw

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def save(self, commit=True):
        u = super().save(commit=False)
        if self.cleaned_data.get("password"):
            u.set_password(self.cleaned_data["password"])
        u.is_staff = True
        if commit:
            u.save()
        return u


class RoleForm(SModelForm):
    permissions = forms.MultipleChoiceField(choices=[(k, v) for k, v in PERMISSIONS.items()], widget=forms.CheckboxSelectMultiple, required=False)

    class Meta:
        model = Role
        fields = ["name", "permissions"]


class ReviewForm(SModelForm):
    class Meta:
        model = Review
        fields = ["name", "location", "rating", "text", "show"]
