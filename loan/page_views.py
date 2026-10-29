from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember

from .document_views import document_requests, document_types, loan_documents
from .draw_views import loan_draws
from .note_views import loan_notes
from .serializers import (
    DocumentRequestSerializer, DocumentTypeSerializer, DrawRequestSerializer, LoanDetailSerializer,
    LoanDocumentSerializer, LoanNoteSerializer,
)
from .views import loan_stages, visible_loans


class LoanPageView(APIView):
    """GET (staff) everything the staff loan page shows, in one response.

    Each part is what its own endpoint returns, so after a change the page refreshes just
    that section from there:
      loan               /api/loans/<pk>/ (with contact_details and named custom fields)
      stages             /api/loans/stages/
      draws, draw_summary          /api/loans/<pk>/draws/, .../draws/summary/
      document_requests, documents /api/loans/<pk>/document-requests/, .../documents/
      document_types     /api/loans/document-types/
      notes              /api/loans/<pk>/notes/
    404 for loans the user can't see (see visible_loans).
    """

    permission_classes = [IsStaffMember]

    def get(self, request, pk):
        user = request.user
        loan = get_object_or_404(visible_loans(user), pk=pk)
        context = {"request": request, "loan": loan}
        return Response({
            "loan": LoanDetailSerializer(loan, context=context).data,
            "stages": loan_stages(),
            "draws": DrawRequestSerializer(loan_draws(loan, user), many=True, context=context).data,
            "draw_summary": loan.draw_summary(),
            "document_requests": DocumentRequestSerializer(
                document_requests(loan, user), many=True, context=context,
            ).data,
            "documents": LoanDocumentSerializer(loan_documents(loan, user), many=True, context=context).data,
            "document_types": DocumentTypeSerializer(document_types(user), many=True, context=context).data,
            "notes": LoanNoteSerializer(loan_notes(loan), many=True, context=context).data,
        })
