// VyaparAI — store portal controller, wired to the live WhatsApp ordering backend.
// All data (store profile, catalogue, orders, notifications, chats) comes from the API; SSE keeps it live.
import {
  api, clearToken, esc, rupees, waToHtml, initials, todayStr, addDays, timeAgo,
  mapProduct, mapOrder, mapNotification, mapStore, storeToSettings, groupOrdersByDate,
  STATUS_TO_API, connectEvents,
} from './api.js';

const CLOSED = ['DELIVERED', 'REJECTED', 'CANCELLED'];
const isActive = (o) => !CLOSED.includes(o.status);
const countsRevenue = (o) => o.status !== 'REJECTED' && o.status !== 'CANCELLED';
const revenueOf = (orders) => orders.reduce((sum, o) => sum + (countsRevenue(o) ? o.totalAmount : 0), 0);
const LOW_STOCK = 10;

class VyaparStoreApp {
  constructor() {
    this.today = todayStr();
    this.activeView = 'dashboard';
    this.selectedDate = this.today;
    this.orderFilter = 'all';
    this.inventorySearchQuery = '';
    this.inventoryCategoryFilter = 'all';
    this.inventoryStockFilter = 'all';
    this.inventoryAvailFilter = 'all';
    this.notifTypeFilter = 'all';

    // live data (filled from the API)
    this.settings = {};
    this.store = mapStore({ store_name: 'Loading…' });
    this.products = [];
    this.orders = {};
    this.notifications = [];
    this.customers = [];

    // modal / chat context
    this.selectedOrderId = null;
    this.selectedProductId = null;
    this.selectedChatId = null;
    this.incomingOrderToastTimer = null;
    this.activeFloatingOrder = null;
    this.pendingCoordinates = null;

    // WhatsApp simulator identity (kept per browser so the conversation survives reloads)
    this.simPhone = this.readLocal('vyapar_sim_phone') || this.newSimPhone();   // customers are per shop, so one id is fine
    this.simName = 'You (bot preview)';

    this.init();
  }

  async init() {
    try {
      this.me = await api.get('/api/auth/me');
    } catch {
      location.href = './login.html';
      return;
    }
    this.cacheDom();
    this.bindEvents();
    this.renderAccount();
    this.render();
    try {
      await this.refreshAll();
    } catch (e) {
      this.showToastNotification(`⚠️ Can't reach the server: ${e.message}`, true);
    }
    if (new URLSearchParams(location.search).has('welcome')) {
      history.replaceState(null, '', location.pathname);
      this.navigateToView('settings');
      this.showToastNotification(`🎉 ${this.me.shop.name} is live! Share your WhatsApp link below to get orders.`);
    }
    connectEvents((event, data) => this.onLiveEvent(event, data), (ok) => this.setLiveStatus(ok));
    setInterval(() => this.rollDayIfNeeded(), 60000);
  }

  // =========================================================================
  // Data loading (server is the single source of truth)
  // =========================================================================

  async refreshAll() {
    await Promise.all([this.loadStore(), this.loadProducts(), this.loadOrders(), this.loadNotifications(), this.loadCustomers()]);
    this.render();
  }

  async loadStore() {
    this.settings = await api.get('/api/settings');
    this.store = mapStore(this.settings);
  }

  async loadProducts() {
    this.products = (await api.get('/api/products')).map(mapProduct);
  }

  async loadOrders() {
    this.orders = groupOrdersByDate((await api.get('/api/orders?limit=2000')).map(mapOrder));
    this.populateDatePickers();
  }

  async loadNotifications() {
    this.notifications = (await api.get('/api/notifications?limit=150')).map(mapNotification);
  }

  async loadCustomers() {
    this.customers = await api.get('/api/customers');
  }

  async resetToDefault() {
    await this.refreshAll();
    this.showToastNotification('Reloaded the latest store data from the server.');
  }

  readLocal(key) { try { return localStorage.getItem(key); } catch { return null; } }
  writeLocal(key, v) { try { localStorage.setItem(key, v); } catch {} }

  newSimPhone() {
    const phone = `sim-portal-${Math.floor(100000 + Math.random() * 900000)}`;
    this.writeLocal('vyapar_sim_phone', phone);
    return phone;
  }

  rollDayIfNeeded() {
    const now = todayStr();
    if (now !== this.today) {
      const wasToday = this.selectedDate === this.today;
      this.today = now;
      if (wasToday) this.selectedDate = now;
      this.populateDatePickers();
    }
    this.renderNotificationsView();
  }

  findOrder(orderId) {
    for (const list of Object.values(this.orders)) {
      const match = list.find((o) => o.id === String(orderId));
      if (match) return match;
    }
    return null;
  }

  // =========================================================================
  // Live events from the backend (Server-Sent Events)
  // =========================================================================

  setLiveStatus(ok) {
    this.aiAgentSubtext.textContent = ok
      ? (this.store.isOpen ? 'Catalog synced • Live' : this.aiAgentSubtext.textContent)
      : 'Reconnecting to server…';
  }

  async onLiveEvent(event, data) {
    switch (event) {
      case 'order_new': {
        await this.loadOrders();
        this.render();
        const order = this.findOrder(data.id);
        if (order) {
          this.playChime();
          this.showFloatingOrderToast(order);
        }
        break;
      }
      case 'order_updated':
        await this.loadOrders();
        this.render();
        if (this.orderDetailsModal.style.display === 'flex' && this.selectedOrderId === String(data.id)) {
          this.openOrderDetailsModal(this.selectedOrderId);
        }
        break;
      case 'inventory':
        await this.loadProducts();
        this.render();
        break;
      case 'notification':
        this.notifications.unshift(mapNotification(data));
        this.renderBadges();
        this.renderNotificationsView();
        this.renderDashboardHome();
        if (['low_stock', 'out_of_stock', 'customer_message'].includes(data.type)) {
          this.showToastNotification(data.title);
        }
        break;
      case 'settings':
        this.settings = data;
        this.store = mapStore(data);
        this.render();
        break;
      case 'message':
        this.onLiveMessage(data);
        break;
      case 'attention':
      case 'customer':
        clearTimeout(this.customerReloadTimer);
        this.customerReloadTimer = setTimeout(async () => {
          await this.loadCustomers();
          this.renderBadges();
          if (this.activeView === 'chats') this.renderChatList();
        }, 250);
        break;
    }
  }

  onLiveMessage(m) {
    // WhatsApp simulator modal
    if (m.phone === this.simPhone && m.direction === 'out' && m.kind !== 'alert') {
      this.setSimTyping(false);
      this.appendWaMessage('bot', m);
    }
    // Customer Chats view
    if (this.activeView === 'chats' && m.customer_id === this.selectedChatId) {
      this.appendChatBubble(m);
    }
    clearTimeout(this.customerReloadTimer);
    this.customerReloadTimer = setTimeout(async () => {
      await this.loadCustomers();
      this.renderBadges();
      if (this.activeView === 'chats') this.renderChatList();
    }, 300);
  }

  playChime() {
    try {
      this.orderAudio.currentTime = 0;
      this.orderAudio.play().catch(() => this.beep());
    } catch { this.beep(); }
  }

  beep() {
    try {
      const ctx = new AudioContext(), o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.value = 0.08;
      o.start(); o.frequency.setValueAtTime(1175, ctx.currentTime + 0.12); o.stop(ctx.currentTime + 0.3);
    } catch {}
  }

  // =========================================================================
  // DOM Caching
  // =========================================================================

  cacheDom() {
    const $ = (id) => document.getElementById(id);
    // Navigation
    this.navItems = document.querySelectorAll('.nav-item');
    this.viewPanels = document.querySelectorAll('.view-panel');
    this.mobileMenuBtn = $('mobileMenuBtn');
    this.sidebarCloseBtn = $('sidebarCloseBtn');
    this.sidebar = $('sidebar');

    // Header & meta
    this.sidebarBrandAvatar = $('sidebarBrandAvatar');
    this.sidebarStoreName = $('sidebarStoreName');
    this.sidebarBusinessType = $('sidebarBusinessType');
    this.topbarStoreName = $('topbarStoreName');
    this.topbarBusinessType = $('topbarBusinessType');
    this.topbarStoreLocation = $('topbarStoreLocation');
    this.storeStatusContainer = $('storeStatusContainer');
    this.storeOfflineBanner = $('storeOfflineBanner');
    this.bannerReasonText = $('bannerReasonText');
    this.bannerReopenBtn = $('bannerReopenBtn');
    this.globalDatePicker = $('globalDatePicker');
    this.analysisDatePicker = $('analysisDatePicker');

    // AI status
    this.aiAgentStatusText = $('aiAgentStatusText');
    this.aiAgentSubtext = $('aiAgentSubtext');
    this.miniAiStatus = $('miniAiStatus');
    this.miniAvgResponse = $('miniAvgResponse');
    this.aiChannelNumber = $('aiChannelNumber');

    // Badges & notifications dropdown
    this.activeOrdersCountBadge = $('activeOrdersCountBadge');
    this.lowStockCountBadge = $('lowStockCountBadge');
    this.chatAttentionBadge = $('chatAttentionBadge');
    this.sidebarNotifBadge = $('sidebarNotifBadge');
    this.bellBadge = $('bellBadge');
    this.bellToggleBtn = $('bellToggleBtn');
    this.notifDropdown = $('notifDropdown');
    this.quickNotifList = $('quickNotifList');
    this.markAllReadBtnTop = $('markAllReadBtnTop');
    this.viewAllNotifsBtn = $('viewAllNotifsBtn');

    // Dashboard
    this.heroStoreTitle = $('heroStoreTitle');
    this.valTodayOrders = $('valTodayOrders');
    this.lblOrdersMetric = $('lblOrdersMetric');
    this.subTodayOrders = $('subTodayOrders');
    this.trendOrders = $('trendOrders');
    this.valTodayRevenue = $('valTodayRevenue');
    this.revAvgPerOrder = $('revAvgPerOrder');
    this.lblRevenueMetric = $('lblRevenueMetric');
    this.tagLiveRevenue = $('tagLiveRevenue');
    this.subTodayRevenue = $('subTodayRevenue');
    this.valTotalProducts = $('valTotalProducts');
    this.chipAvailableProducts = $('chipAvailableProducts');
    this.subInventoryAlerts = $('subInventoryAlerts');
    this.valStoreStatus = $('valStoreStatus');
    this.subStoreStatus = $('subStoreStatus');
    this.statusCardIconWrap = $('statusCardIconWrap');
    this.cardTodayRevenue = $('cardTodayRevenue');
    this.dashboardRecentOrdersTbody = $('dashboardRecentOrdersTbody');
    this.dashboardLowStockList = $('dashboardLowStockList');
    this.dashboardRecentNotifFeed = $('dashboardRecentNotifFeed');
    this.btnGoOrdersView = $('btnGoOrdersView');
    this.btnGoInventoryView = $('btnGoInventoryView');
    this.btnGoNotifsView = $('btnGoNotifsView');
    this.btnGoAddProduct = $('btnGoAddProduct');

    // Orders
    this.ordersMainTbody = $('ordersMainTbody');
    this.orderFilterTabs = $('orderFilterTabs');
    this.filterCountAll = $('filterCountAll');
    this.filterCountActive = $('filterCountActive');
    this.filterCountNew = $('filterCountNew');
    this.filterCountPrep = $('filterCountPrep');
    this.filterCountReady = $('filterCountReady');
    this.filterCountDone = $('filterCountDone');
    this.btnShareFromOrders = $('btnShareFromOrders');

    // Customer chats
    this.chatSearchInput = $('chatSearchInput');
    this.chatList = $('chatList');
    this.chatThreadCol = $('chatThreadCol');

    // Upload details (store setup)
    this.storeDetailsForm = $('storeDetailsForm');
    this.inputStoreName = $('inputStoreName');
    this.selectBusinessType = $('selectBusinessType');
    this.customBusinessTypeWrap = $('customBusinessTypeWrap');
    this.inputCustomBusinessType = $('inputCustomBusinessType');
    this.inputStorePhone = $('inputStorePhone');
    this.inputStoreAddress = $('inputStoreAddress');
    this.inputStoreCity = $('inputStoreCity');
    this.inputStorePincode = $('inputStorePincode');
    this.inputStoreDesc = $('inputStoreDesc');
    this.inputOpeningTime = $('inputOpeningTime');
    this.inputClosingTime = $('inputClosingTime');
    this.logoPreviewCircle = $('logoPreviewCircle');
    this.logoInitials = $('logoInitials');
    this.logoCurrentLabel = $('logoCurrentLabel');
    this.logoFileInput = $('logoFileInput');
    this.previewStoreName = $('previewStoreName');
    this.previewBusinessType = $('previewBusinessType');
    this.previewPhone = $('previewPhone');
    this.btnUseCurrentLocationSetup = $('btnUseCurrentLocationSetup');
    this.btnResetDefaultStore = $('btnResetDefaultStore');
    this.btnTriggerStoreSave = $('btnTriggerStoreSave');
    this.btnDiscardSetup = $('btnDiscardSetup');
    this.storeSaveSuccessBanner = $('storeSaveSuccessBanner');
    this.btnCloseSuccessBanner = $('btnCloseSuccessBanner');
    this.presetPills = document.querySelectorAll('.preset-pill');

    // Inventory
    this.inventorySearchInput = $('inventorySearchInput');
    this.btnClearInventorySearch = $('btnClearInventorySearch');
    this.filterCategory = $('filterCategory');
    this.filterStockStatus = $('filterStockStatus');
    this.filterAvailability = $('filterAvailability');
    this.inventoryTbody = $('inventoryTbody');
    this.btnOpenAddProductModal = $('btnOpenAddProductModal');

    // Analysis
    this.analysisSelectedDateBadge = $('analysisSelectedDateBadge');
    this.analysisTotalAmount = $('analysisTotalAmount');
    this.analysisRevenueStatusPill = $('analysisRevenueStatusPill');
    this.analysisOrderCount = $('analysisOrderCount');
    this.analysisAvgOrder = $('analysisAvgOrder');
    this.analysisItemsSold = $('analysisItemsSold');
    this.historicalRevenueBars = $('historicalRevenueBars');
    this.analysisCategoryBars = $('analysisCategoryBars');
    this.analysisTopItemsList = $('analysisTopItemsList');

    // Notifications
    this.fullNotifList = $('fullNotifList');
    this.btnMarkAllReadSection = $('btnMarkAllReadSection');
    this.notifTabs = document.querySelectorAll('.notif-tab');

    // Settings
    this.settingsStoreName = $('settingsStoreName');
    this.settingsBusinessType = $('settingsBusinessType');
    this.settingsCustomBusinessTypeWrap = $('settingsCustomBusinessTypeWrap');
    this.settingsCustomBusinessType = $('settingsCustomBusinessType');
    this.settingsStorePhone = $('settingsStorePhone');
    this.settingsOpeningTime = $('settingsOpeningTime');
    this.settingsClosingTime = $('settingsClosingTime');
    this.settingsStoreAddress = $('settingsStoreAddress');
    this.settingsStoreCity = $('settingsStoreCity');
    this.settingsStorePincode = $('settingsStorePincode');
    this.locationStatusText = $('locationStatusText');
    this.btnUseCurrentLocationSettings = $('btnUseCurrentLocationSettings');
    this.btnSaveStoreSettingsTab = $('btnSaveStoreSettingsTab');
    this.settingsAiGreeting = $('settingsAiGreeting');
    this.settingsDeliveryEta = $('settingsDeliveryEta');
    this.settingsUpiId = $('settingsUpiId');
    this.settingsDeliveryFee = $('settingsDeliveryFee');
    this.settingsFreeDeliveryAbove = $('settingsFreeDeliveryAbove');
    this.settingsMinOrderVal = $('settingsMinOrderVal');

    // Modals
    this.closeStoreModal = $('closeStoreModal');
    this.btnCancelCloseStore = $('btnCancelCloseStore');
    this.btnConfirmCloseStore = $('btnConfirmCloseStore');
    this.customCloseReasonWrap = $('customCloseReasonWrap');
    this.customCloseReasonInput = $('customCloseReasonInput');
    this.closeReasonRadios = document.querySelectorAll('input[name="closeReason"]');

    this.orderDetailsModal = $('orderDetailsModal');
    this.btnCloseOrderDetailsModal = $('btnCloseOrderDetailsModal');
    this.modalOrderNumber = $('modalOrderNumber');
    this.modalOrderStatus = $('modalOrderStatus');
    this.modalOrderCustomerTitle = $('modalOrderCustomerTitle');
    this.modalCustomerName = $('modalCustomerName');
    this.modalCustomerPhone = $('modalCustomerPhone');
    this.modalOrderTime = $('modalOrderTime');
    this.modalOrderPayment = $('modalOrderPayment');
    this.modalCustomerAddress = $('modalCustomerAddress');
    this.modalItemsTbody = $('modalItemsTbody');
    this.modalTotalAmount = $('modalTotalAmount');
    this.modalOrderExtras = $('modalOrderExtras');
    this.modalOrderFooterActions = $('modalOrderFooterActions');

    this.rejectOrderModal = $('rejectOrderModal');
    this.rejectOrderSubtitle = $('rejectOrderSubtitle');
    this.btnCancelRejectOrder = $('btnCancelRejectOrder');
    this.btnConfirmRejectOrder = $('btnConfirmRejectOrder');
    this.customRejectReasonWrap = $('customRejectReasonWrap');
    this.customRejectReasonInput = $('customRejectReasonInput');
    this.rejectReasonRadios = document.querySelectorAll('input[name="rejectReason"]');

    this.productModal = $('productModal');
    this.productModalTitle = $('productModalTitle');
    this.btnCloseProductModal = $('btnCloseProductModal');
    this.btnCancelProductModal = $('btnCancelProductModal');
    this.productForm = $('productForm');
    this.editProductId = $('editProductId');
    this.prodNameInput = $('prodNameInput');
    this.prodCategoryInput = $('prodCategoryInput');
    this.prodBrandInput = $('prodBrandInput');
    this.prodVariantInput = $('prodVariantInput');
    this.prodPriceInput = $('prodPriceInput');
    this.prodQuantityInput = $('prodQuantityInput');
    this.prodAvailabilitySelect = $('prodAvailabilitySelect');
    this.prodDescInput = $('prodDescInput');

    this.deleteProductModal = $('deleteProductModal');
    this.deleteProductSubtitle = $('deleteProductSubtitle');
    this.btnCancelDeleteProduct = $('btnCancelDeleteProduct');
    this.btnConfirmDeleteProduct = $('btnConfirmDeleteProduct');

    this.revenueModal = $('revenueModal');
    this.revenueModalTitle = $('revenueModalTitle');
    this.btnCloseRevenueModal = $('btnCloseRevenueModal');
    this.btnCloseRevenueModalBottom = $('btnCloseRevenueModalBottom');
    this.revModalAmount = $('revModalAmount');
    this.revModalOrders = $('revModalOrders');
    this.revModalAvg = $('revModalAvg');
    this.revModalUPI = $('revModalUPI');
    this.revModalCash = $('revModalCash');
    this.revModalPillLabel = $('revModalPillLabel');
    this.revHistoryTbody = $('revHistoryTbody');

    // WhatsApp simulator + floating new-order toast
    this.whatsappSimulatorModal = $('whatsappSimulatorModal');
    this.btnCloseWhatsAppSimulator = $('btnCloseWhatsAppSimulator');
    this.btnOpenWhatsAppSimulator = $('btnOpenWhatsAppSimulator');
    this.waChatMessages = $('waChatMessages');
    this.waCustomerInput = $('waCustomerInput');
    this.btnSendWaMsg = $('btnSendWaMsg');
    this.btnPromptLocation = $('btnPromptLocation');
    this.btnSimNewCustomer = $('btnSimNewCustomer');

    this.floatingOrderToast = $('floatingOrderToast');
    this.toastOrderNumber = $('toastOrderNumber');
    this.toastCustomerName = $('toastCustomerName');
    this.toastCustomerPhone = $('toastCustomerPhone');
    this.toastCustomerAddress = $('toastCustomerAddress');
    this.toastItemsSummary = $('toastItemsSummary');
    this.toastTotalAmount = $('toastTotalAmount');
    this.btnToastAccept = $('btnToastAccept');
    this.btnToastReject = $('btnToastReject');
    this.orderAudio = $('orderNotificationAudio');
  }

  // =========================================================================
  // Event Binding
  // =========================================================================

  bindEvents() {
    this.navItems.forEach((btn) => btn.addEventListener('click', () => this.navigateToView(btn.dataset.view)));

    this.mobileMenuBtn?.addEventListener('click', () => this.sidebar.classList.add('mobile-open'));
    this.sidebarCloseBtn?.addEventListener('click', () => this.sidebar.classList.remove('mobile-open'));

    this.btnGoOrdersView?.addEventListener('click', () => this.navigateToView('orders'));
    this.btnGoInventoryView?.addEventListener('click', () => this.navigateToView('inventory'));
    this.btnGoNotifsView?.addEventListener('click', () => this.navigateToView('notifications'));

    const onDate = (value) => {
      this.selectedDate = value;
      this.globalDatePicker.value = value;
      this.analysisDatePicker.value = value;
      this.render();
    };
    this.globalDatePicker.addEventListener('change', (e) => onDate(e.target.value));
    this.analysisDatePicker.addEventListener('change', (e) => onDate(e.target.value));

    // Notification dropdown
    this.bellToggleBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      this.notifDropdown.classList.toggle('show');
    });
    document.addEventListener('click', (e) => {
      if (!this.notifDropdown.contains(e.target) && !this.bellToggleBtn.contains(e.target)) {
        this.notifDropdown.classList.remove('show');
      }
    });
    this.markAllReadBtnTop.addEventListener('click', () => this.markAllNotificationsRead());
    this.btnMarkAllReadSection.addEventListener('click', () => this.markAllNotificationsRead());
    this.viewAllNotifsBtn.addEventListener('click', () => {
      this.notifDropdown.classList.remove('show');
      this.navigateToView('notifications');
    });

    // Revenue deep-dive
    this.cardTodayRevenue.addEventListener('click', () => this.openRevenueModal());
    this.btnCloseRevenueModal.addEventListener('click', () => this.closeModal(this.revenueModal));
    this.btnCloseRevenueModalBottom.addEventListener('click', () => this.closeModal(this.revenueModal));

    // Store open / close
    this.bannerReopenBtn.addEventListener('click', () => this.reopenStore());
    this.closeReasonRadios.forEach((radio) => radio.addEventListener('change', (e) => {
      const other = e.target.value === 'Other';
      this.customCloseReasonWrap.style.display = other ? 'block' : 'none';
      if (other) this.customCloseReasonInput.focus();
    }));
    this.btnCancelCloseStore.addEventListener('click', () => this.closeModal(this.closeStoreModal));
    this.btnConfirmCloseStore.addEventListener('click', () => this.confirmCloseStore());

    // Orders
    this.orderFilterTabs?.addEventListener('click', (e) => {
      const tab = e.target.closest('.pill-tab');
      if (!tab) return;
      this.orderFilterTabs.querySelectorAll('.pill-tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      this.orderFilter = tab.dataset.filter;
      this.renderOrdersTable();
    });
    this.btnCloseOrderDetailsModal.addEventListener('click', () => this.closeModal(this.orderDetailsModal));

    this.rejectReasonRadios.forEach((radio) => radio.addEventListener('change', (e) => {
      const other = e.target.value === 'Other';
      this.customRejectReasonWrap.style.display = other ? 'block' : 'none';
      if (other) this.customRejectReasonInput.focus();
    }));
    this.btnCancelRejectOrder.addEventListener('click', () => this.closeModal(this.rejectOrderModal));
    this.btnConfirmRejectOrder.addEventListener('click', () => this.confirmRejectOrder());

    // Store setup form
    this.selectBusinessType.addEventListener('change', (e) => {
      if (e.target.value === 'Other') {
        this.customBusinessTypeWrap.style.display = 'block';
        this.inputCustomBusinessType.focus();
        this.presetPills.forEach((p) => p.classList.toggle('active', p.dataset.preset === 'other'));
      } else {
        this.customBusinessTypeWrap.style.display = 'none';
        const typeMap = { 'Kirana / General Store': 'kirana', Restaurant: 'restaurant', 'Café': 'cafe', Bakery: 'bakery', 'Medical Shop': 'medical' };
        const preset = typeMap[e.target.value] || null;
        this.presetPills.forEach((p) => p.classList.toggle('active', p.dataset.preset === preset));
      }
      this.updateSetupLivePreview();
    });
    this.inputStoreName.addEventListener('input', () => this.updateSetupLivePreview());
    this.inputStorePhone.addEventListener('input', () => this.updateSetupLivePreview());
    this.inputCustomBusinessType.addEventListener('input', () => this.updateSetupLivePreview());
    this.storeDetailsForm.addEventListener('input', () => { this.storeFormDirty = true; });
    this.storeDetailsForm.addEventListener('submit', (e) => {
      e.preventDefault();
      this.handleStoreDetailsSave();
    });
    this.btnTriggerStoreSave.addEventListener('click', () => this.storeDetailsForm.requestSubmit());
    this.btnDiscardSetup.addEventListener('click', () => {
      this.storeFormDirty = false;
      this.populateUploadDetailsForm();
      this.showToastNotification('Changes reverted to last saved details.');
    });
    this.btnResetDefaultStore.addEventListener('click', () => this.resetToDefault());
    this.btnCloseSuccessBanner.addEventListener('click', () => { this.storeSaveSuccessBanner.style.display = 'none'; });
    this.presetPills.forEach((pill) => pill.addEventListener('click', () => {
      this.presetPills.forEach((p) => p.classList.remove('active'));
      pill.classList.add('active');
      this.applyPreset(pill.dataset.preset);
    }));
    this.logoFileInput.addEventListener('change', (e) => this.handleLogoUpload(e));
    this.btnUseCurrentLocationSetup.addEventListener('click', () => this.handleUseCurrentLocation('setup'));
    this.btnUseCurrentLocationSettings.addEventListener('click', () => this.handleUseCurrentLocation('settings'));
    this.btnSaveStoreSettingsTab.addEventListener('click', () => this.handleSaveSettingsTab());
    this.settingsBusinessType?.addEventListener('change', (e) => {
      const other = e.target.value === 'Other';
      if (this.settingsCustomBusinessTypeWrap) this.settingsCustomBusinessTypeWrap.style.display = other ? 'block' : 'none';
      if (other) this.settingsCustomBusinessType.focus();
    });

    // Inventory
    this.inventorySearchInput.addEventListener('input', (e) => {
      this.inventorySearchQuery = e.target.value.toLowerCase().trim();
      this.btnClearInventorySearch.style.display = this.inventorySearchQuery ? 'block' : 'none';
      this.renderInventoryTable();
    });
    this.btnClearInventorySearch.addEventListener('click', () => {
      this.inventorySearchInput.value = '';
      this.inventorySearchQuery = '';
      this.btnClearInventorySearch.style.display = 'none';
      this.renderInventoryTable();
    });
    this.filterCategory.addEventListener('change', (e) => { this.inventoryCategoryFilter = e.target.value; this.renderInventoryTable(); });
    this.filterStockStatus.addEventListener('change', (e) => { this.inventoryStockFilter = e.target.value; this.renderInventoryTable(); });
    this.filterAvailability.addEventListener('change', (e) => { this.inventoryAvailFilter = e.target.value; this.renderInventoryTable(); });
    this.btnOpenAddProductModal.addEventListener('click', () => this.openProductModal());
    this.btnCloseProductModal.addEventListener('click', () => this.closeModal(this.productModal));
    this.btnCancelProductModal.addEventListener('click', () => this.closeModal(this.productModal));
    this.productForm.addEventListener('submit', (e) => {
      e.preventDefault();
      this.handleProductFormSubmit();
    });
    this.btnCancelDeleteProduct.addEventListener('click', () => this.closeModal(this.deleteProductModal));
    this.btnConfirmDeleteProduct.addEventListener('click', () => this.confirmDeleteProduct());

    // Notifications
    this.notifTabs.forEach((tab) => tab.addEventListener('click', () => {
      this.notifTabs.forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      this.notifTypeFilter = tab.dataset.type;
      this.renderNotificationsView();
    }));

    // Real actions instead of demo data
    const addFirstProduct = () => { this.navigateToView('inventory'); this.openProductModal(); };
    this.btnGoAddProduct.addEventListener('click', addFirstProduct);
    this.btnShareFromOrders.addEventListener('click', () => this.copyJoinLink());
    this.inventoryTbody.addEventListener('click', (e) => { if (e.target.closest('[data-add-first]')) addFirstProduct(); });

    // Floating new-order toast
    this.btnToastAccept.addEventListener('click', () => {
      if (!this.activeFloatingOrder) return;
      this.advanceOrderStatus(this.activeFloatingOrder.id, 'ACCEPTED');
      this.hideFloatingOrderToast();
    });
    this.btnToastReject.addEventListener('click', () => {
      if (!this.activeFloatingOrder) return;
      const orderId = this.activeFloatingOrder.id;
      this.hideFloatingOrderToast();
      this.openRejectOrderModal(orderId);
    });

    // Customer chats
    this.chatSearchInput.addEventListener('input', () => this.renderChatList());
    this.chatList.addEventListener('click', (e) => {
      const item = e.target.closest('[data-chat-id]');
      if (item) this.openChat(Number(item.dataset.chatId));
    });

    // Account & sharing
    document.getElementById('btnLogout').addEventListener('click', () => this.logout());
    document.getElementById('btnCopyJoinLink').addEventListener('click', () => this.copyJoinLink());
    document.getElementById('btnCopyShareLink').addEventListener('click', () => this.copyJoinLink());
    document.getElementById('btnPrintPoster').addEventListener('click', () => this.printPoster());
    document.getElementById('btnSaveAccount').addEventListener('click', () => this.saveAccount());
    document.getElementById('btnChangePassword').addEventListener('click', () => this.changePassword());
    document.getElementById('btnSaveOwnNumber').addEventListener('click', () => this.saveOwnNumber());

    // WhatsApp simulator (talks to the real bot)
    this.btnOpenWhatsAppSimulator.addEventListener('click', () => this.openSimulator());
    this.btnCloseWhatsAppSimulator.addEventListener('click', () => this.closeModal(this.whatsappSimulatorModal));
    this.btnSendWaMsg.addEventListener('click', () => this.handleSendWhatsAppMessage());
    this.waCustomerInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') this.handleSendWhatsAppMessage(); });
    // quick prompts (static "Hi" + real in-stock product names, filled when the preview opens)
    document.querySelector('.wa-quick-prompts').addEventListener('click', (e) => {
      const b = e.target.closest('.btn-prompt[data-prompt]');
      if (b) this.simSend({ text: b.dataset.prompt });
    });
    this.btnPromptLocation.addEventListener('click', () => this.simSend({
      latitude: 17.5946, longitude: 78.4412, address: 'MRCET Road, Maisammaguda, Dulapally, Hyderabad', text: '📍 Location',
    }));
    const btnHandwritten = document.getElementById('btnPromptHandwritten');
    if (btnHandwritten) {
      btnHandwritten.addEventListener('click', async () => {
        try {
          this.setSimTyping(true);
          this.appendWaMessage('customer', { body: '📷 Sent handwritten shopping list (parcha)', created_at: new Date().toISOString() });
          const res = await fetch('/handwritten_parcha.jpg');
          const blob = await res.blob();
          const reader = new FileReader();
          reader.onloadend = () => {
            const b64 = reader.result.split(',')[1];
            this.simSend({ image_base64: b64, image_mime: 'image/jpeg' });
          };
          reader.readAsDataURL(blob);
        } catch (e) {
          this.setSimTyping(false);
          this.appendSystemNote(`⚠️ Failed to load sample handwritten note: ${e.message}`);
        }
      });
    }
    const btnAttach = document.getElementById('btnAttachImage');
    const fileInput = document.getElementById('waFileInput');
    if (btnAttach && fileInput) {
      btnAttach.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', () => {
        const file = fileInput.files[0];
        if (!file) return;
        this.appendWaMessage('customer', { body: `📷 Uploaded: ${file.name}`, created_at: new Date().toISOString() });
        const reader = new FileReader();
        reader.onloadend = () => {
          const b64 = reader.result.split(',')[1];
          this.simSend({ image_base64: b64, image_mime: file.type || 'image/jpeg' });
          fileInput.value = '';
        };
        reader.readAsDataURL(file);
      });
    }
    this.btnSimNewCustomer.addEventListener('click', () => {
      this.simPhone = this.newSimPhone();
      this.waChatMessages.innerHTML = '';
      this.appendSystemNote('New customer started — say "Hi" 👋');
    });
    this.waChatMessages.addEventListener('click', (e) => this.onSimOptionClick(e));
  }

  // =========================================================================
  // Account, shop link & QR
  // =========================================================================

  renderAccount() {
    const { user, shop, join } = this.me;
    const ini = initials(user.name || user.email);
    document.getElementById('userAvatar').textContent = ini;
    document.getElementById('staffAvatar').textContent = ini;
    document.getElementById('userName').textContent = user.name || user.email;
    document.getElementById('staffName').textContent = (user.name || '').split(' ')[0] || 'Owner';
    document.getElementById('userRole').textContent = `Owner · ${shop.slug}`;
    document.getElementById('heroJoinCode').textContent = join.join_text;
    document.getElementById('accountEmail').textContent = `Logged in as ${user.email}`;
    document.getElementById('accountName').value = user.name || '';
    document.getElementById('accountPhone').value = user.phone || '';
    document.getElementById('ownNumberId').value = shop.wa_phone_number_id || '';
    document.getElementById('ownNumberStatus').textContent = shop.has_own_number
      ? '✅ Connected — customers message this number directly.' : 'Using the shared platform number.';

    const link = join.wa_link;
    document.getElementById('shareLinkBox').textContent = link || `Customers send “${join.join_text}” to the shop's WhatsApp number`;
    const open = document.getElementById('btnOpenShareLink');
    open.style.display = link ? '' : 'none';
    if (link) open.href = link;
    document.getElementById('shareSteps').innerHTML = join.dedicated_number
      ? '<li>Customers message your WhatsApp number — any text starts the menu.</li><li>Orders appear here instantly with a chime.</li>'
      : `<li>Customer scans the QR or taps the link — WhatsApp opens with <b>${esc(join.join_text)}</b> typed in.</li>
         <li>They press send and get <b>${esc(shop.name)}</b>'s menu — your catalogue only.</li>
         <li>Orders, chats and payments show up here instantly.</li>
         ${join.whatsapp_number ? `<li>WhatsApp number: <b>${esc(join.whatsapp_number)}</b> · shop code <b>${esc(join.code)}</b></li>` : ''}`;
    const qrEl = document.getElementById('shareQr');
    qrEl.innerHTML = '';
    if (link && window.QRCode) new window.QRCode(qrEl, { text: link, width: 140, height: 140, correctLevel: window.QRCode.CorrectLevel.M });
    else qrEl.innerHTML = '<span style="color:#111; font-size:0.75rem; text-align:center; padding:8px;">QR appears once the WhatsApp number is known</span>';
  }

  async copyJoinLink() {
    const { join } = this.me;
    const text = join.wa_link || join.join_text;
    try {
      await navigator.clipboard.writeText(text);
      this.showToastNotification(join.wa_link ? 'Ordering link copied — paste it in your WhatsApp status!' : `Copied “${text}”`);
    } catch {
      window.prompt('Copy your ordering link:', text);
    }
  }

  printPoster() {
    const { shop, join } = this.me;
    const qrImg = document.querySelector('#shareQr img, #shareQr canvas');
    const src = qrImg ? (qrImg.tagName === 'CANVAS' ? qrImg.toDataURL() : qrImg.src) : '';
    const w = window.open('', '_blank', 'width=600,height=800');
    if (!w) return this.showToastNotification('⚠️ Allow pop-ups to print the poster', true);
    w.document.write(`<!doctype html><html><head><title>${esc(shop.name)} — Order on WhatsApp</title>
      <style>body{font-family:system-ui,sans-serif;text-align:center;padding:40px;color:#111}h1{font-size:34px;margin:0 0 6px}
      .tag{display:inline-block;background:#25d366;color:#fff;font-weight:800;padding:8px 18px;border-radius:999px;font-size:20px;margin:14px 0}
      img{width:300px;height:300px;margin:18px auto;display:block}.code{font-family:monospace;font-size:24px;background:#f1f5f9;padding:8px 16px;border-radius:10px;display:inline-block}
      p{font-size:18px;color:#334155}</style></head><body>
      <h1>${esc(shop.name)}</h1><div class="tag">📲 Order on WhatsApp</div>
      ${src ? `<img src="${src}" alt="QR">` : ''}
      <p>Scan with your phone camera, then press <b>Send</b>.</p>
      ${join.dedicated_number ? '' : `<p>Or message ${esc(join.whatsapp_number || 'our WhatsApp number')}:</p><div class="code">${esc(join.join_text)}</div>`}
      <script>window.onload=()=>setTimeout(()=>window.print(),300)<\/script></body></html>`);
    w.document.close();
  }

  async saveAccount() {
    try {
      this.me = await api.put('/api/auth/account', {
        name: document.getElementById('accountName').value, phone: document.getElementById('accountPhone').value,
      });
      this.renderAccount();
      this.showToastNotification('Profile saved.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  async changePassword() {
    const err = document.getElementById('errPassword');
    err.textContent = '';
    const current = document.getElementById('pwCurrent').value;
    const next = document.getElementById('pwNew').value;
    if (next.length < 8) { err.textContent = 'New password needs at least 8 characters'; return; }
    try {
      await api.put('/api/auth/password', { current_password: current, new_password: next });
      document.getElementById('pwCurrent').value = '';
      document.getElementById('pwNew').value = '';
      this.showToastNotification('Password changed — other devices were signed out.');
    } catch (e) { err.textContent = e.message; }
  }

  async saveOwnNumber() {
    try {
      this.me = await api.put('/api/shop/whatsapp', {
        phone_number_id: document.getElementById('ownNumberId').value,
        access_token: document.getElementById('ownNumberToken').value,
      });
      document.getElementById('ownNumberToken').value = '';
      this.renderAccount();
      this.showToastNotification(this.me.shop.has_own_number ? 'Own WhatsApp number connected.' : 'Saved — using the shared number.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  async logout() {
    try { await api.post('/api/auth/logout'); } catch {}
    clearToken();
    location.href = './login.html';
  }

  // =========================================================================
  // Navigation
  // =========================================================================

  navigateToView(viewName) {
    this.activeView = viewName;
    this.navItems.forEach((item) => item.classList.toggle('active', item.dataset.view === viewName));
    this.viewPanels.forEach((panel) => panel.classList.toggle('active', panel.id === `view-${viewName}`));
    this.sidebar.classList.remove('mobile-open');
    if (viewName === 'chats') {
      this.loadCustomers().then(() => {
        this.renderChatList();
        if (this.selectedChatId) this.openChat(this.selectedChatId);
      });
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // =========================================================================
  // Master render
  // =========================================================================

  render() {
    this.renderStoreMeta();
    this.renderStoreStatusControl();
    this.renderBadges();
    this.renderDashboardHome();
    this.renderOrdersTable();
    if (!this.storeFormDirty) this.populateUploadDetailsForm();
    this.renderInventoryTable();
    this.renderAnalysisView();
    this.renderNotificationsView();
    if (this.activeView !== 'settings') this.populateSettingsTab();
  }

  getEffectiveBusinessType() {
    if (this.store.businessType === 'Other') return this.store.customBusinessType || 'Other';
    return this.store.businessType || 'Kirana / General Store';
  }

  populateDatePickers() {
    const dates = new Set(Object.keys(this.orders));
    for (let i = 0; i < 7; i++) dates.add(addDays(this.today, -i));
    const sorted = [...dates].filter((d) => d <= this.today).sort().reverse();
    const options = sorted.map((d) => {
      const n = (this.orders[d] || []).length;
      return `<option value="${d}" ${d === this.selectedDate ? 'selected' : ''}>${this.formatDisplayDate(d)}${n ? ` · ${n} orders` : ''}</option>`;
    }).join('');
    this.globalDatePicker.innerHTML = options;
    this.analysisDatePicker.innerHTML = options;
  }

  // =========================================================================
  // Store meta & branding
  // =========================================================================

  renderStoreMeta() {
    const businessName = this.store.name || 'My Store';
    const bType = this.getEffectiveBusinessType();
    const avatarText = this.sidebarBrandAvatar.querySelector('.brand-avatar-text');

    this.sidebarStoreName.textContent = businessName;
    this.sidebarBusinessType.textContent = bType;
    this.sidebarBrandAvatar.style.background = this.store.logoBg || '#059669';
    if (this.store.logoUrl) {
      this.sidebarBrandAvatar.style.backgroundImage = `url(${this.store.logoUrl})`;
      this.sidebarBrandAvatar.style.backgroundSize = 'cover';
      avatarText.textContent = '';
    } else {
      this.sidebarBrandAvatar.style.backgroundImage = '';
      avatarText.textContent = this.store.logoInitials || initials(businessName);
    }

    this.topbarStoreName.textContent = businessName;
    this.topbarBusinessType.textContent = bType;
    this.topbarStoreLocation.textContent = `📍 ${this.store.city || '—'}${this.store.pincode ? ` • ${this.store.pincode}` : ''}`;
    this.heroStoreTitle.textContent = `Welcome to ${businessName}`;
    document.title = `${businessName} — VyaparAI Store Portal`;
    this.aiChannelNumber.textContent = this.store.phone ? `WhatsApp: ${this.store.phone}` : 'WhatsApp ordering bot';
    this.miniAvgResponse.textContent = this.customers.filter((c) => c.channel === 'whatsapp').length || this.customers.length;

    const waHeaderStoreName = document.getElementById('waHeaderStoreName');
    if (waHeaderStoreName) waHeaderStoreName.textContent = businessName;
    const waAvatarIcon = document.getElementById('waAvatarIcon');
    if (waAvatarIcon) waAvatarIcon.textContent = this.store.logoInitials || initials(businessName);
  }

  // =========================================================================
  // Store status (open / closed) — the WhatsApp bot pauses ordering while closed
  // =========================================================================

  renderStoreStatusControl() {
    const reasonDisplay = this.store.closeReason || 'Temporarily unavailable';
    if (this.store.isOpen) {
      this.storeStatusContainer.className = 'store-status-control is-open';
      this.storeStatusContainer.innerHTML = `
        <span class="status-badge-live"><span class="live-dot pulse-green"></span><span>STORE OPEN</span></span>
        <button class="btn-status-action btn-close-store" id="btnTriggerCloseModal" title="Close store for new orders">Close Store</button>`;
      document.getElementById('btnTriggerCloseModal').addEventListener('click', () => this.openCloseStoreModal());
      this.storeOfflineBanner.style.display = 'none';
      this.aiAgentStatusText.textContent = 'WhatsApp Bot: Accepting Orders';
      this.aiAgentSubtext.textContent = 'Catalog synced • Live';
      this.miniAiStatus.textContent = 'ONLINE';
      this.miniAiStatus.style.color = '#6ee7b7';
      this.valStoreStatus.textContent = 'STORE OPEN';
      this.valStoreStatus.className = 'metric-card-value text-emerald';
      this.subStoreStatus.textContent = 'Customers can order now via WhatsApp';
      this.statusCardIconWrap.innerHTML = '<span class="status-indicator-dot pulse-green"></span>';
      this.statusCardIconWrap.className = 'metric-icon-wrap bg-emerald-subtle';
    } else {
      this.storeStatusContainer.className = 'store-status-control is-closed';
      this.storeStatusContainer.innerHTML = `
        <span class="status-badge-live"><span class="live-dot pulse-red"></span><span>STORE CLOSED</span></span>
        <button class="btn-status-action btn-open-store" id="btnTriggerOpenStore" title="Open store for orders">Reopen Store</button>`;
      document.getElementById('btnTriggerOpenStore').addEventListener('click', () => this.reopenStore());
      this.storeOfflineBanner.style.display = 'flex';
      this.bannerReasonText.textContent = `Reason: ${reasonDisplay}`;
      this.aiAgentStatusText.textContent = 'WhatsApp Bot: Orders Paused';
      this.aiAgentSubtext.textContent = `Reason: ${reasonDisplay}`;
      this.miniAiStatus.textContent = 'PAUSED';
      this.miniAiStatus.style.color = '#fb7185';
      this.valStoreStatus.textContent = 'STORE CLOSED';
      this.valStoreStatus.className = 'metric-card-value text-rose';
      this.subStoreStatus.textContent = `Reason: ${reasonDisplay}`;
      this.statusCardIconWrap.innerHTML = '<span class="status-indicator-dot pulse-red"></span>';
      this.statusCardIconWrap.className = 'metric-icon-wrap bg-rose-subtle';
    }
  }

  openCloseStoreModal() {
    this.customCloseReasonWrap.style.display = 'none';
    this.customCloseReasonInput.value = '';
    const firstRadio = document.querySelector('input[name="closeReason"][value="Temporarily unavailable"]');
    if (firstRadio) firstRadio.checked = true;
    this.openModal(this.closeStoreModal);
  }

  async confirmCloseStore() {
    const selected = document.querySelector('input[name="closeReason"]:checked');
    const reason = selected ? selected.value : 'Temporarily unavailable';
    const custom = this.customCloseReasonInput.value.trim();
    const reasonText = reason === 'Other' ? (custom || 'Temporarily unavailable') : reason;
    try {
      this.settings = await api.post('/api/store/status', { is_open: false, reason: reasonText });
      this.store = mapStore(this.settings);
      this.closeModal(this.closeStoreModal);
      this.render();
      this.showToastNotification(`Store closed — WhatsApp orders paused (${reasonText}).`);
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  async reopenStore() {
    try {
      this.settings = await api.post('/api/store/status', { is_open: true });
      this.store = mapStore(this.settings);
      this.render();
      this.showToastNotification('Store is OPEN — the WhatsApp bot is taking orders.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  // =========================================================================
  // Badges & notification center
  // =========================================================================

  renderBadges() {
    this.activeOrdersCountBadge.textContent = Object.values(this.orders).flat().filter(isActive).length;

    const lowStockCount = this.products.filter((p) => p.quantity <= LOW_STOCK).length;
    this.lowStockCountBadge.style.display = lowStockCount ? 'inline-block' : 'none';
    this.lowStockCountBadge.textContent = lowStockCount;

    const attention = this.customers.filter((c) => c.needs_attention).length;
    this.chatAttentionBadge.style.display = attention ? 'inline-block' : 'none';
    this.chatAttentionBadge.textContent = attention;

    const unread = this.notifications.filter((n) => !n.isRead).length;
    this.bellBadge.style.display = unread ? 'flex' : 'none';
    this.bellBadge.textContent = unread;
    this.sidebarNotifBadge.textContent = unread;

    this.quickNotifList.innerHTML = this.notifications.slice(0, 6).map((n) => `
      <div class="quick-notif-item ${!n.isRead ? 'unread' : ''}" data-id="${n.id}">
        <span class="quick-notif-icon">${this.getNotificationIcon(n.type)}</span>
        <div class="quick-notif-body">
          <span class="quick-notif-title">${esc(n.title)}</span>
          <span class="quick-notif-msg">${esc(n.message)}</span>
          <span class="quick-notif-time">${n.time}</span>
        </div>
      </div>`).join('') || '<div style="padding:16px; color:var(--text-muted); font-size:0.8rem;">No notifications yet.</div>';
    this.quickNotifList.querySelectorAll('.quick-notif-item').forEach((item) => {
      item.addEventListener('click', () => this.openNotification(item.dataset.id));
    });
  }

  getNotificationIcon(type) {
    return {
      new_order: '🔔', low_stock: '⚠️', out_of_stock: '❌', payment_received: '💰', order_dispatched: '🛵',
      order_delivered: '✅', order_cancelled: '🛑', order_rejected: '🚫', customer_message: '💬',
      store_status: '🏪', product_added: '📦',
    }[type] || '📢';
  }

  async openNotification(id) {
    const n = this.notifications.find((x) => x.id === id);
    if (!n) return;
    this.markNotificationRead(id);
    this.notifDropdown.classList.remove('show');
    if (n.orderId && this.findOrder(n.orderId)) this.openOrderDetailsModal(n.orderId);
    else if (n.customerId) { this.selectedChatId = n.customerId; this.navigateToView('chats'); }
    else if (n.productId) { this.navigateToView('inventory'); }
  }

  async markAllNotificationsRead() {
    this.notifications.forEach((n) => { n.isRead = true; });
    this.renderBadges();
    this.renderNotificationsView();
    try { await api.post('/api/notifications/read-all'); } catch {}
    this.showToastNotification('All notifications marked as read.');
  }

  async markNotificationRead(id) {
    const n = this.notifications.find((x) => x.id === id);
    if (!n || n.isRead) return;
    n.isRead = true;
    this.renderBadges();
    this.renderNotificationsView();
    try { await api.post(`/api/notifications/${id}/read`); } catch {}
  }

  // =========================================================================
  // Dashboard
  // =========================================================================

  renderDashboardHome() {
    const dayOrders = this.orders[this.selectedDate] || [];
    const isToday = this.selectedDate === this.today;

    this.valTodayOrders.textContent = dayOrders.length;
    this.lblOrdersMetric.textContent = isToday ? "Today's Orders" : `Orders on ${this.formatDisplayDate(this.selectedDate)}`;
    const activeCount = dayOrders.filter(isActive).length;
    this.subTodayOrders.textContent = `${activeCount} pending in queue • ${dayOrders.length - activeCount} completed`;

    const prevOrders = (this.orders[addDays(this.selectedDate, -1)] || []).length;
    if (prevOrders) {
      const pct = Math.round(((dayOrders.length - prevOrders) / prevOrders) * 100);
      this.trendOrders.textContent = `${pct >= 0 ? '+' : ''}${pct}% vs prev. day`;
      this.trendOrders.className = `metric-trend ${pct >= 0 ? 'positive' : 'negative'}`;
    } else {
      this.trendOrders.textContent = dayOrders.length ? 'First orders 🎉' : 'No orders yet';
      this.trendOrders.className = 'metric-trend positive';
    }

    const totalRev = revenueOf(dayOrders);
    const counted = dayOrders.filter(countsRevenue).length;
    this.valTodayRevenue.textContent = rupees(totalRev);
    this.revAvgPerOrder.textContent = `Avg ${rupees(counted ? Math.round(totalRev / counted) : 0)}/ord`;
    if (isToday) {
      this.lblRevenueMetric.textContent = "Today's Revenue So Far";
      this.tagLiveRevenue.style.display = 'inline-block';
      this.subTodayRevenue.textContent = 'Live • Click for breakdown →';
    } else {
      this.lblRevenueMetric.textContent = 'Final Daily Revenue';
      this.tagLiveRevenue.style.display = 'none';
      this.subTodayRevenue.textContent = `Ledger for ${this.formatDisplayDate(this.selectedDate)} →`;
    }

    this.valTotalProducts.textContent = this.products.length;
    this.chipAvailableProducts.textContent = `${this.products.filter((p) => p.isAvailable && p.quantity > 0).length} Active`;
    const lowStock = this.products.filter((p) => p.quantity > 0 && p.quantity <= LOW_STOCK).length;
    const outStock = this.products.filter((p) => p.quantity <= 0).length;
    this.subInventoryAlerts.textContent = `${lowStock} Low Stock • ${outStock} Out of Stock`;

    const topRecent = this.sortOrdersForPriority(dayOrders).slice(0, 6);
    if (!topRecent.length) {
      this.dashboardRecentOrdersTbody.innerHTML = `
        <tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">
          No orders for ${this.formatDisplayDate(this.selectedDate)} yet. Orders placed on WhatsApp appear here instantly.
        </td></tr>`;
    } else {
      this.dashboardRecentOrdersTbody.innerHTML = topRecent.map((o) => this.renderOrderRow(o, true)).join('');
      this.attachOrderTableEventListeners(this.dashboardRecentOrdersTbody);
    }

    const lowStockItems = this.products.filter((p) => p.quantity <= LOW_STOCK).sort((a, b) => a.quantity - b.quantity);
    this.dashboardLowStockList.innerHTML = !this.products.length
      ? '<div style="padding: 16px; text-align: center; color: var(--text-muted); font-size: 0.8rem;">📦 No products yet — <b>+ Add products</b> to start taking WhatsApp orders.</div>'
      : !lowStockItems.length
      ? '<div style="padding: 16px; text-align: center; color: var(--text-muted); font-size: 0.8rem;">🟢 All items are well-stocked.</div>'
      : lowStockItems.slice(0, 5).map((p) => `
        <div class="low-stock-item">
          <div class="low-stock-info">
            <span class="low-stock-name">${esc(p.name)} (${esc(p.variant)})</span>
            <span class="low-stock-meta">${esc(p.brand || p.category)}</span>
          </div>
          <div class="low-stock-status">
            <span class="low-stock-qty ${p.quantity <= 0 ? 'text-rose' : 'text-amber'}">${p.quantity} left</span>
            <span class="badge-stock ${p.quantity <= 0 ? 'stock-red' : 'stock-yellow'}">${p.quantity <= 0 ? 'Out of Stock' : 'Low Stock'}</span>
          </div>
        </div>`).join('');

    this.dashboardRecentNotifFeed.innerHTML = this.notifications.slice(0, 4).map((n) => `
      <div class="feed-item ${n.type === 'low_stock' ? 'feed-warn' : n.type === 'out_of_stock' ? 'feed-danger' : ''}">
        <div class="feed-content">
          <div class="feed-title">${esc(n.title)}</div>
          <div class="feed-desc">${esc(n.message)}</div>
          <div class="feed-time">${n.time}</div>
        </div>
      </div>`).join('') || '<div style="padding: 16px; color: var(--text-muted); font-size: 0.8rem;">Activity will show up here.</div>';
  }

  // =========================================================================
  // Orders (priority queue)
  // =========================================================================

  renderOrdersTable() {
    const dayOrders = this.orders[this.selectedDate] || [];
    this.filterCountAll.textContent = dayOrders.length;
    this.filterCountActive.textContent = dayOrders.filter(isActive).length;
    this.filterCountNew.textContent = dayOrders.filter((o) => o.status === 'NEW').length;
    this.filterCountPrep.textContent = dayOrders.filter((o) => o.status === 'PREPARING' || o.status === 'ACCEPTED').length;
    this.filterCountReady.textContent = dayOrders.filter((o) => o.status === 'READY').length;
    this.filterCountDone.textContent = dayOrders.filter((o) => CLOSED.includes(o.status)).length;

    let filtered = dayOrders;
    if (this.orderFilter === 'active') filtered = dayOrders.filter(isActive);
    else if (this.orderFilter === 'new') filtered = dayOrders.filter((o) => o.status === 'NEW');
    else if (this.orderFilter === 'preparing') filtered = dayOrders.filter((o) => o.status === 'PREPARING' || o.status === 'ACCEPTED');
    else if (this.orderFilter === 'ready') filtered = dayOrders.filter((o) => o.status === 'READY');
    else if (this.orderFilter === 'completed') filtered = dayOrders.filter((o) => CLOSED.includes(o.status));

    const prioritized = this.sortOrdersForPriority(filtered);
    if (!prioritized.length) {
      this.ordersMainTbody.innerHTML = `
        <tr><td colspan="8" style="text-align: center; padding: 40px; color: var(--text-muted);">
          No orders match this filter for ${this.formatDisplayDate(this.selectedDate)}.
        </td></tr>`;
      return;
    }
    this.ordersMainTbody.innerHTML = prioritized.map((o) => this.renderOrderRow(o, false)).join('');
    this.attachOrderTableEventListeners(this.ordersMainTbody);
  }

  sortOrdersForPriority(list) {
    const weight = { NEW: 1, ACCEPTED: 2, PREPARING: 3, READY: 4, OUT_FOR_DELIVERY: 5, DELIVERED: 6, REJECTED: 7, CANCELLED: 8 };
    return [...list].sort((a, b) => ((weight[a.status] || 99) - (weight[b.status] || 99)) || (a.timestamp - b.timestamp));
  }

  renderOrderRow(order, isCompact) {
    const itemsCount = order.items.reduce((s, it) => s + it.qty, 0);
    const payTag = order.paymentState === 'paid' ? ' <span title="Paid" style="font-size:0.7rem;">💰</span>' : '';
    return `
      <tr class="${order.status === 'NEW' ? 'tr-new-order' : ''}" data-order-id="${order.id}">
        <td><span class="order-num-pill">${esc(order.orderNumber)}</span></td>
        <td>
          <div class="customer-meta">
            <span class="customer-name">${esc(order.customerName)}</span>
            ${isCompact ? `<span class="customer-phone">${esc(order.customerPhone)}</span>` : ''}
          </div>
        </td>
        ${!isCompact ? `<td><span class="customer-phone">${esc(order.customerPhone)}</span></td>` : ''}
        <td>
          <button class="order-items-btn" data-action="view-items" data-order-id="${order.id}" title="View items, address and payment">
            <span>${itemsCount} ${itemsCount === 1 ? 'item' : 'items'}</span><span>👁️</span>
          </button>
        </td>
        <td><span class="order-amount">${rupees(order.totalAmount)}</span>${payTag}</td>
        <td><span style="font-size:0.78rem; color:var(--text-secondary); white-space:nowrap;">${order.time}</span></td>
        <td><span class="status-pill ${this.getStatusBadgeClass(order.status)}">${this.formatStatusLabel(order.status)}</span></td>
        <td>${this.renderWorkflowActionButtons(order)}</td>
      </tr>`;
  }

  getStatusBadgeClass(status) {
    return {
      NEW: 'status-new', ACCEPTED: 'status-accepted', PREPARING: 'status-preparing', READY: 'status-ready',
      OUT_FOR_DELIVERY: 'status-out-for-delivery', DELIVERED: 'status-delivered', REJECTED: 'status-rejected', CANCELLED: 'status-rejected',
    }[status] || '';
  }

  formatStatusLabel(status) {
    return {
      NEW: 'New', ACCEPTED: 'Accepted', PREPARING: 'Preparing', READY: 'Ready', OUT_FOR_DELIVERY: 'Out for Delivery',
      DELIVERED: 'Delivered', REJECTED: 'Rejected', CANCELLED: 'Cancelled',
    }[status] || status;
  }

  renderWorkflowActionButtons(order) {
    const id = order.id;
    switch (order.status) {
      case 'NEW':
        return `<div class="action-btn-group">
          <button class="btn-workflow-primary" data-action="accept" data-order-id="${id}">Accept</button>
          <button class="btn-workflow-reject" data-action="reject" data-order-id="${id}">Reject</button></div>`;
      case 'ACCEPTED':
        return `<div class="action-btn-group"><button class="btn-workflow-prep" data-action="start-prep" data-order-id="${id}">Start Preparing</button></div>`;
      case 'PREPARING':
        return `<div class="action-btn-group"><button class="btn-workflow-ready" data-action="mark-ready" data-order-id="${id}">Mark Ready</button></div>`;
      case 'READY':
        return `<div class="action-btn-group"><button class="btn-workflow-dispatch" data-action="out-for-delivery" data-order-id="${id}">Out for Delivery</button></div>`;
      case 'OUT_FOR_DELIVERY':
        return `<div class="action-btn-group"><button class="btn-workflow-primary" data-action="mark-delivered" data-order-id="${id}">Mark Delivered</button></div>`;
      case 'DELIVERED':
        return '<span style="font-size:0.75rem; color:var(--emerald-500); font-weight:700;">✓ Completed</span>';
      case 'REJECTED':
      case 'CANCELLED':
        return `<span style="font-size:0.72rem; color:var(--rose-500);">${esc(order.rejectReason || this.formatStatusLabel(order.status))}</span>`;
      default:
        return '';
    }
  }

  attachOrderTableEventListeners(container) {
    container.querySelectorAll('[data-action]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const { action, orderId } = btn.dataset;
        const next = { accept: 'ACCEPTED', 'start-prep': 'PREPARING', 'mark-ready': 'READY', 'out-for-delivery': 'OUT_FOR_DELIVERY', 'mark-delivered': 'DELIVERED' }[action];
        if (action === 'view-items') this.openOrderDetailsModal(orderId);
        else if (action === 'reject') this.openRejectOrderModal(orderId);
        else if (action === 'mark-paid') this.setPayment(orderId, 'paid');
        else if (action === 'mark-refunded') this.setPayment(orderId, 'refunded');
        else if (action === 'open-chat') {
          const order = this.findOrder(orderId);
          this.closeModal(this.orderDetailsModal);
          this.selectedChatId = order?.customerId;
          this.navigateToView('chats');
        } else if (next) this.advanceOrderStatus(orderId, next, btn);
      });
    });
  }

  // Status changes go to the backend, which updates stock and messages the customer on WhatsApp.
  async advanceOrderStatus(orderId, nextStatus, btn) {
    const order = this.findOrder(orderId);
    if (!order) return;
    if (btn) btn.disabled = true;
    try {
      await api.post(`/api/orders/${orderId}/status`, {
        status: STATUS_TO_API[nextStatus],
        eta: nextStatus === 'ACCEPTED' ? (this.settings.delivery_eta || null) : null,
      });
      await this.loadOrders();
      this.render();
      this.showToastNotification(`${order.orderNumber} → ${this.formatStatusLabel(nextStatus)} · customer notified on WhatsApp`);
      if (this.orderDetailsModal.style.display === 'flex' && this.selectedOrderId === orderId) this.openOrderDetailsModal(orderId);
    } catch (e) {
      if (btn) btn.disabled = false;
      this.showToastNotification(`⚠️ ${e.message}`, true);
    }
  }

  async setPayment(orderId, status) {
    try {
      await api.post(`/api/orders/${orderId}/payment`, { status });
      await this.loadOrders();
      this.render();
      if (this.selectedOrderId === orderId) this.openOrderDetailsModal(orderId);
      this.showToastNotification(status === 'paid' ? 'Payment marked as received.' : 'Refund recorded.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  openOrderDetailsModal(orderId) {
    const order = this.findOrder(orderId);
    if (!order) return;
    this.selectedOrderId = order.id;

    this.modalOrderNumber.textContent = order.orderNumber;
    this.modalOrderStatus.textContent = this.formatStatusLabel(order.status);
    this.modalOrderStatus.className = `status-pill-modal ${this.getStatusBadgeClass(order.status)}`;
    const typeBadge = document.getElementById('modalStoreTypeBadge');
    if (typeBadge) typeBadge.textContent = order.paymentMethod === 'UPI' ? '📲 UPI' : '💵 Cash on Delivery';
    this.modalOrderCustomerTitle.textContent = `Order from ${order.customerName}`;
    this.modalCustomerName.textContent = order.customerName;
    this.modalCustomerPhone.textContent = order.customerPhone;
    this.modalOrderTime.textContent = `${order.time} · ${this.formatDisplayDate(order.date)}`;
    this.modalOrderPayment.textContent = order.paymentStatus;
    this.modalCustomerAddress.innerHTML = esc(order.customerAddress) + (order.lat != null
      ? ` <a href="https://maps.google.com/?q=${order.lat},${order.lng}" target="_blank" rel="noopener" class="btn-link">Open in Maps ↗</a>` : '');

    this.modalItemsTbody.innerHTML = order.items.map((item) => `
      <tr>
        <td><strong>${esc(item.name)}</strong>${item.variant ? `<small style="color:var(--text-muted); display:block;">${esc(item.variant)}</small>` : ''}</td>
        <td>${item.qty} ×</td>
        <td>${rupees(item.price)}</td>
        <td style="text-align:right; font-weight:700;">${rupees(item.qty * item.price)}</td>
      </tr>`).join('') + (order.deliveryFee ? `
      <tr><td colspan="3" style="color:var(--text-muted);">Delivery fee</td><td style="text-align:right;">${rupees(order.deliveryFee)}</td></tr>` : '');
    this.modalTotalAmount.textContent = rupees(order.totalAmount);

    const labels = {
      pending: 'Order placed on WhatsApp', accepted: 'Accepted by store', preparing: 'Preparing', packed: 'Ready / packed',
      out_for_delivery: 'Out for delivery', delivered: 'Delivered', rejected: 'Rejected', cancelled: 'Cancelled',
      payment_paid: 'Payment received', payment_refunded: 'Refunded', payment_refund_due: 'Refund due',
    };
    this.modalOrderExtras.innerHTML = `
      <h4 class="subhead-items mt-4">Order timeline</h4>
      <div class="order-timeline">
        ${order.timeline.map((t) => `
          <div class="timeline-step">
            <span class="timeline-dot"></span>
            <div><strong>${esc(labels[t.status] || t.status)}</strong>${t.note && t.note !== labels[t.status] ? ` <span class="timeline-note">— ${esc(t.note)}</span>` : ''}
              <div class="timeline-time">${new Date(t.created_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })} · by ${esc(t.actor || 'system')}</div></div>
          </div>`).join('')}
      </div>`;

    const payBtn = order.paymentState === 'pending' && !['REJECTED', 'CANCELLED'].includes(order.status)
      ? `<button class="btn btn-outline" data-action="mark-paid" data-order-id="${order.id}">💰 Mark Paid</button>`
      : order.paymentState === 'refund_due' ? `<button class="btn btn-outline" data-action="mark-refunded" data-order-id="${order.id}">↩️ Mark Refunded</button>` : '';
    this.modalOrderFooterActions.innerHTML = `
      <button class="btn btn-outline" id="btnCloseOrderDetailsInner">Close</button>
      <button class="btn btn-outline" data-action="open-chat" data-order-id="${order.id}">💬 Chat with customer</button>
      ${payBtn}
      ${this.renderWorkflowActionButtons(order)}`;
    document.getElementById('btnCloseOrderDetailsInner').addEventListener('click', () => this.closeModal(this.orderDetailsModal));
    this.attachOrderTableEventListeners(this.modalOrderFooterActions);
    this.openModal(this.orderDetailsModal);
  }

  openRejectOrderModal(orderId) {
    const order = this.findOrder(orderId);
    if (!order) return;
    this.selectedOrderId = order.id;
    this.rejectOrderSubtitle.textContent = `Order ${order.orderNumber} • ${order.customerName} (${rupees(order.totalAmount)})`;
    this.customRejectReasonWrap.style.display = 'none';
    this.customRejectReasonInput.value = '';
    const firstRadio = document.querySelector('input[name="rejectReason"][value="Item unavailable"]');
    if (firstRadio) firstRadio.checked = true;
    this.openModal(this.rejectOrderModal);
  }

  async confirmRejectOrder() {
    if (!this.selectedOrderId) return;
    const selected = document.querySelector('input[name="rejectReason"]:checked');
    const reason = selected ? selected.value : 'Item unavailable';
    const custom = this.customRejectReasonInput.value.trim();
    const finalReason = reason === 'Other' && custom ? custom : reason;
    const order = this.findOrder(this.selectedOrderId);
    try {
      await api.post(`/api/orders/${this.selectedOrderId}/status`, { status: 'rejected', note: finalReason });
      this.closeModal(this.rejectOrderModal);
      this.closeModal(this.orderDetailsModal);
      await Promise.all([this.loadOrders(), this.loadProducts()]);
      this.render();
      this.showToastNotification(`${order?.orderNumber || 'Order'} rejected — customer informed on WhatsApp.`);
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  // =========================================================================
  // Upload Details — store setup
  // =========================================================================

  populateUploadDetailsForm() {
    const standardTypes = ['Kirana / General Store', 'Restaurant', 'Café', 'Bakery', 'Medical Shop', 'Grocery Store', 'Clothing Store', 'Electronics Store'];
    this.inputStoreName.value = this.store.name || '';
    const presetMap = { 'Kirana / General Store': 'kirana', Restaurant: 'restaurant', 'Café': 'cafe', Bakery: 'bakery', 'Medical Shop': 'medical' };
    const matchingPreset = presetMap[this.store.businessType] || (standardTypes.includes(this.store.businessType) ? null : 'other');
    this.presetPills.forEach((p) => p.classList.toggle('active', p.dataset.preset === matchingPreset));
    if (standardTypes.includes(this.store.businessType)) {
      this.selectBusinessType.value = this.store.businessType;
      this.customBusinessTypeWrap.style.display = 'none';
    } else {
      this.selectBusinessType.value = 'Other';
      this.customBusinessTypeWrap.style.display = 'block';
      this.inputCustomBusinessType.value = this.store.customBusinessType || this.store.businessType;
    }
    this.inputStorePhone.value = this.store.phone || '';
    this.inputStoreAddress.value = this.store.address || '';
    this.inputStoreCity.value = this.store.city || '';
    this.inputStorePincode.value = this.store.pincode || '';
    this.inputStoreDesc.value = this.store.description || '';
    this.inputOpeningTime.value = this.store.openingTime || '07:00';
    this.inputClosingTime.value = this.store.closingTime || '22:00';
    this.renderLogoPreview();
    this.updateSetupLivePreview();
  }

  renderLogoPreview() {
    if (this.store.logoUrl) {
      this.logoPreviewCircle.style.backgroundImage = `url(${this.store.logoUrl})`;
      this.logoPreviewCircle.style.backgroundSize = 'cover';
      this.logoInitials.textContent = '';
    } else {
      this.logoPreviewCircle.style.backgroundImage = '';
    }
  }

  updateSetupLivePreview() {
    const name = this.inputStoreName.value.trim() || 'My Store';
    let bType = this.selectBusinessType.value;
    if (bType === 'Other') bType = this.inputCustomBusinessType.value.trim() || 'Other';
    this.previewStoreName.textContent = name;
    this.previewBusinessType.textContent = bType;
    this.previewPhone.textContent = this.inputStorePhone.value.trim() || '—';
    if (!this.store.logoUrl) this.logoInitials.textContent = initials(name);
    this.logoCurrentLabel.textContent = `Current: ${name}`;
  }

  applyPreset(presetKey) {
    const presets = {
      kirana: ['Kirana / General Store', 'Your trusted neighbourhood kirana store for fresh groceries, dairy, bakery items and snacks — order on WhatsApp.', '#059669'],
      restaurant: ['Restaurant', 'Biryani, starters, curries and rotis prepared fresh on order. Order on WhatsApp with live kitchen updates.', '#ea580c'],
      cafe: ['Café', 'Coffee, sandwiches, cold brews and desserts — order ahead on WhatsApp.', '#7c2d12'],
      bakery: ['Bakery', 'Fresh breads, pastries, cakes and cookies baked every morning.', '#d97706'],
      medical: ['Medical Shop', 'Licensed chemist and wellness store. Order medicines and essentials on WhatsApp.', '#0284c7'],
    };
    if (presetKey === 'other') {
      this.selectBusinessType.value = 'Other';
      this.customBusinessTypeWrap.style.display = 'block';
      setTimeout(() => this.inputCustomBusinessType.focus(), 30);
    } else if (presets[presetKey]) {
      const [type, desc, color] = presets[presetKey];
      this.selectBusinessType.value = type;
      this.customBusinessTypeWrap.style.display = 'none';
      this.inputCustomBusinessType.value = '';
      this.inputStoreDesc.value = desc;
      this.pendingLogoBg = color;
    }
    this.updateSetupLivePreview();
  }

  handleLogoUpload(e) {
    const file = e.target.files[0];
    if (!file) return;
    // shrink to a 160px square so it's tiny enough to store with the settings
    const img = new Image();
    const reader = new FileReader();
    reader.onload = (ev) => { img.src = ev.target.result; };
    img.onload = async () => {
      const size = 160, canvas = document.createElement('canvas');
      canvas.width = canvas.height = size;
      const scale = Math.max(size / img.width, size / img.height);
      const w = img.width * scale, h = img.height * scale;
      canvas.getContext('2d').drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      try {
        this.settings = await api.put('/api/settings', { logo_image: dataUrl });
        this.store = mapStore(this.settings);
        this.renderStoreMeta();
        this.renderLogoPreview();
        this.showToastNotification('Store logo updated.');
      } catch (err) { this.showToastNotification(`⚠️ ${err.message}`, true); }
    };
    reader.readAsDataURL(file);
  }

  async handleStoreDetailsSave() {
    const name = this.inputStoreName.value.trim();
    const bType = this.selectBusinessType.value;
    const customBType = this.inputCustomBusinessType.value.trim();
    const phone = this.inputStorePhone.value.trim();
    const address = this.inputStoreAddress.value.trim();
    const city = this.inputStoreCity.value.trim();
    const pincode = this.inputStorePincode.value.trim();

    const errors = {
      errStoreName: !name && 'Please enter your store name.',
      errCustomBusinessType: bType === 'Other' && !customBType && 'Please enter your business type.',
      errStorePhone: (!phone || phone.replace(/\D/g, '').length < 10) && 'Please enter a valid 10-digit mobile number.',
      errStoreAddress: !address && 'Please enter your complete store address.',
      errStoreCity: !city && 'Please enter your city.',
      errStorePincode: !/^\d{6}$/.test(pincode) && 'Please enter a valid 6-digit PIN code.',
    };
    let hasErrors = false;
    for (const [id, msg] of Object.entries(errors)) {
      const el = document.getElementById(id);
      if (el) el.textContent = msg || '';
      hasErrors ||= !!msg;
    }
    if (hasErrors) return;

    const next = {
      ...this.store, name, businessType: bType, customBusinessType: bType === 'Other' ? customBType : '',
      phone, address, city, pincode, description: this.inputStoreDesc.value.trim(),
      openingTime: this.inputOpeningTime.value, closingTime: this.inputClosingTime.value,
      logoBg: this.pendingLogoBg || this.store.logoBg,
      coordinates: this.pendingCoordinates || this.store.coordinates,
    };
    try {
      this.settings = await api.put('/api/settings', storeToSettings(next));
      this.store = mapStore(this.settings);
      this.pendingLogoBg = null;
      this.storeFormDirty = false;
      this.render();
      this.storeSaveSuccessBanner.style.display = 'flex';
      window.scrollTo({ top: 0, behavior: 'smooth' });
      this.showToastNotification('Store details saved — WhatsApp bot greetings updated.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  handleUseCurrentLocation(targetContext) {
    const done = (coords, note) => {
      this.pendingCoordinates = coords;
      if (targetContext === 'settings') this.locationStatusText.textContent = `Coordinates: ${coords}${note ? ` (${note})` : ''} — click Save to keep`;
      this.showToastNotification(`📍 Location captured: ${coords}`);
    };
    if (!navigator.geolocation) return this.showToastNotification('⚠️ Location not available in this browser.', true);
    navigator.geolocation.getCurrentPosition(
      (pos) => done(`${pos.coords.latitude.toFixed(5)}, ${pos.coords.longitude.toFixed(5)}`, `±${Math.round(pos.coords.accuracy)} m`),
      (err) => this.showToastNotification(`⚠️ Couldn't get location: ${err.message}`, true),
      { enableHighAccuracy: true, timeout: 10000 },
    );
  }

  // =========================================================================
  // Inventory
  // =========================================================================

  renderInventoryTable() {
    const categories = [...new Set(this.products.map((p) => p.category))].sort();
    const current = this.filterCategory.value;
    this.filterCategory.innerHTML = `<option value="all">All Categories (${this.products.length})</option>` +
      categories.map((c) => `<option value="${esc(c)}" ${c === current ? 'selected' : ''}>${esc(c)}</option>`).join('');
    const catList = document.getElementById('prodCategoryList');
    if (catList) catList.innerHTML = categories.map((c) => `<option value="${esc(c)}">`).join('');

    let filtered = this.products;
    if (this.inventorySearchQuery) {
      const q = this.inventorySearchQuery;
      filtered = filtered.filter((p) => [p.name, p.category, p.brand, p.keywords].some((v) => (v || '').toLowerCase().includes(q)));
    }
    if (this.inventoryCategoryFilter !== 'all') filtered = filtered.filter((p) => p.category === this.inventoryCategoryFilter);
    if (this.inventoryStockFilter === 'in_stock') filtered = filtered.filter((p) => p.quantity > LOW_STOCK);
    else if (this.inventoryStockFilter === 'low_stock') filtered = filtered.filter((p) => p.quantity > 0 && p.quantity <= LOW_STOCK);
    else if (this.inventoryStockFilter === 'critical') filtered = filtered.filter((p) => p.quantity <= 0);
    if (this.inventoryAvailFilter === 'available') filtered = filtered.filter((p) => p.isAvailable);
    else if (this.inventoryAvailFilter === 'unavailable') filtered = filtered.filter((p) => !p.isAvailable);

    if (!filtered.length) {
      this.inventoryTbody.innerHTML = this.products.length
        ? '<tr><td colspan="9" style="text-align:center; padding: 36px; color: var(--text-muted);">No products match the selected filters.</td></tr>'
        : `<tr><td colspan="9" class="empty-catalogue">
            <div class="empty-catalogue-icon">📦</div>
            <strong>No products yet</strong>
            <p>Add what you sell — name, size, price and stock. Your WhatsApp bot shows customers only what's in stock,
              and hides items automatically when they run out.</p>
            <button class="btn btn-primary" data-add-first>+ Add your first product</button>
          </td></tr>`;
      return;
    }

    this.inventoryTbody.innerHTML = filtered.map((prod) => `
      <tr data-prod-id="${prod.id}">
        <td>
          <strong style="color:var(--text-primary); font-size:0.9rem;">${esc(prod.name)}</strong>
          ${prod.description ? `<small style="color:var(--text-muted); display:block; max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(prod.description)}</small>` : ''}
        </td>
        <td><span style="font-size:0.8rem; background:#f1f5f9; color:#334155; padding:2px 8px; border-radius:var(--radius-full);">${esc(prod.category)}</span></td>
        <td><span style="color:var(--text-secondary);">${esc(prod.brand) || '—'}</span></td>
        <td><span style="font-weight:600;">${esc(prod.variant)}</span></td>
        <td><strong style="font-family:var(--font-mono); color:#047857;">${rupees(prod.price)}</strong></td>
        <td>
          <div class="quantity-stepper">
            <button class="btn-qty-step" data-action="decrement-stock" data-prod-id="${prod.id}">−</button>
            <span class="qty-val-display ${prod.quantity <= 0 ? 'text-rose' : prod.quantity <= LOW_STOCK ? 'text-amber' : ''}">${prod.quantity}</span>
            <button class="btn-qty-step" data-action="increment-stock" data-prod-id="${prod.id}">+</button>
          </div>
          ${prod.reserved ? `<small class="reserved-note" title="Held for orders that haven't been sent yet">${prod.reserved} on hold · ${Math.max(prod.available, 0)} sellable</small>` : ''}
        </td>
        <td>${this.renderStockStatusBadge(prod.available)}</td>
        <td>
          <button class="status-pill ${prod.isAvailable ? 'status-delivered' : 'status-rejected'}" data-action="toggle-available" data-prod-id="${prod.id}"
            title="Click to ${prod.isAvailable ? 'hide from' : 'show on'} WhatsApp" style="cursor:pointer; border:0;">
            ${prod.isAvailable ? 'Available' : 'Hidden'}
          </button>
        </td>
        <td style="text-align:right;">
          <div class="action-btn-group" style="justify-content: flex-end;">
            <button class="btn btn-outline btn-sm" data-action="edit-product" data-prod-id="${prod.id}">Edit</button>
            <button class="btn-workflow-reject" data-action="delete-product" data-prod-id="${prod.id}">Delete</button>
          </div>
        </td>
      </tr>`).join('');

    this.inventoryTbody.querySelectorAll('[data-action]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const { action, prodId } = btn.dataset;
        if (action === 'increment-stock') this.adjustProductStock(prodId, 1);
        else if (action === 'decrement-stock') this.adjustProductStock(prodId, -1);
        else if (action === 'toggle-available') this.toggleAvailability(prodId);
        else if (action === 'edit-product') this.openProductModal(prodId);
        else if (action === 'delete-product') this.openDeleteProductModal(prodId);
      });
    });
  }

  renderStockStatusBadge(qty) {
    if (qty > LOW_STOCK) return '<span class="badge-stock stock-green">🟢 In Stock</span>';
    if (qty > 0) return '<span class="badge-stock stock-yellow">🟡 Low Stock</span>';
    return '<span class="badge-stock stock-red">🔴 Out of Stock</span>';
  }

  async adjustProductStock(prodId, delta) {
    const prod = this.products.find((p) => p.id === prodId);
    if (!prod) return;
    prod.quantity = Math.max(0, prod.quantity + delta);   // optimistic
    this.renderInventoryTable();
    try {
      Object.assign(prod, mapProduct(await api.patch(`/api/products/${prodId}`, { stock: prod.quantity })));
      this.renderBadges();
      this.renderDashboardHome();
    } catch (e) {
      this.showToastNotification(`⚠️ ${e.message}`, true);
      await this.loadProducts();
      this.renderInventoryTable();
    }
  }

  async toggleAvailability(prodId) {
    const prod = this.products.find((p) => p.id === prodId);
    if (!prod) return;
    try {
      Object.assign(prod, mapProduct(await api.patch(`/api/products/${prodId}`, { active: !prod.isAvailable })));
      this.render();
      this.showToastNotification(`${prod.name} is now ${prod.isAvailable ? 'available on' : 'hidden from'} WhatsApp.`);
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  openProductModal(prodId = null) {
    this.selectedProductId = prodId;
    if (prodId) {
      const prod = this.products.find((p) => p.id === prodId);
      if (!prod) return;
      this.productModalTitle.textContent = `Edit Product: ${prod.name}`;
      this.editProductId.value = prod.id;
      this.prodNameInput.value = prod.name;
      this.prodCategoryInput.value = prod.category;
      this.prodBrandInput.value = prod.brand || '';
      this.prodVariantInput.value = prod.variant;
      this.prodPriceInput.value = prod.price;
      this.prodQuantityInput.value = prod.quantity;
      this.prodAvailabilitySelect.value = String(prod.isAvailable);
      this.prodDescInput.value = prod.description || '';
    } else {
      this.productModalTitle.textContent = 'Add New Product';
      this.productForm.reset();
      this.editProductId.value = '';
      this.prodQuantityInput.value = '50';
      this.prodAvailabilitySelect.value = 'true';
    }
    this.openModal(this.productModal);
  }

  async handleProductFormSubmit() {
    const id = this.editProductId.value;
    const body = {
      name: this.prodNameInput.value.trim(),
      category: this.prodCategoryInput.value.trim(),
      brand: this.prodBrandInput.value.trim(),
      variant: this.prodVariantInput.value.trim(),
      price: parseFloat(this.prodPriceInput.value) || 0,
      stock: parseInt(this.prodQuantityInput.value, 10) || 0,
      active: this.prodAvailabilitySelect.value === 'true',
      description: this.prodDescInput.value.trim(),
    };
    if (!body.name || !body.category || !body.variant || body.price <= 0) {
      this.showToastNotification('⚠️ Name, category, variant and a price above ₹0 are required.', true);
      return;
    }
    try {
      if (id) await api.patch(`/api/products/${id}`, body);
      else await api.post('/api/products', body);
      this.closeModal(this.productModal);
      await this.loadProducts();
      this.render();
      this.showToastNotification(id ? `Updated ${body.name}.` : `"${body.name}" added — the WhatsApp bot can sell it now.`);
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  openDeleteProductModal(prodId) {
    this.selectedProductId = prodId;
    const prod = this.products.find((p) => p.id === prodId);
    if (!prod) return;
    this.deleteProductSubtitle.textContent = `Remove "${prod.name} (${prod.variant})" from your catalogue and WhatsApp ordering?`;
    this.openModal(this.deleteProductModal);
  }

  async confirmDeleteProduct() {
    if (!this.selectedProductId) return;
    try {
      await api.del(`/api/products/${this.selectedProductId}`);
      this.closeModal(this.deleteProductModal);
      await this.loadProducts();
      this.render();
      this.showToastNotification('Product removed from inventory and WhatsApp ordering.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  // =========================================================================
  // Analysis
  // =========================================================================

  renderAnalysisView() {
    const isToday = this.selectedDate === this.today;
    const dayOrders = this.orders[this.selectedDate] || [];
    const counted = dayOrders.filter(countsRevenue);

    this.analysisSelectedDateBadge.textContent = this.formatDisplayDate(this.selectedDate);
    const totalRevenue = revenueOf(dayOrders);
    this.analysisTotalAmount.textContent = rupees(totalRevenue);
    this.analysisRevenueStatusPill.textContent = isToday ? "Today's Revenue So Far (Live)" : 'Final Daily Revenue (Closed Ledger)';
    this.analysisOrderCount.textContent = dayOrders.length;
    this.analysisAvgOrder.textContent = rupees(counted.length ? Math.round(totalRevenue / counted.length) : 0);
    this.analysisItemsSold.textContent = counted.reduce((sum, o) => sum + o.items.reduce((s, it) => s + it.qty, 0), 0);

    // last 7 days, scaled to the best day
    const days = Array.from({ length: 7 }, (_, i) => addDays(this.today, -i));
    const revs = days.map((d) => revenueOf(this.orders[d] || []));
    const maxRev = Math.max(...revs, 1);
    this.historicalRevenueBars.innerHTML = days.map((date, i) => {
      const isLive = date === this.today;
      return `
        <div class="history-bar-row ${date === this.selectedDate ? 'is-selected' : ''}" data-date="${date}">
          <span class="bar-date-label">${this.formatDisplayDate(date)}</span>
          <div class="bar-track"><div class="bar-fill ${isLive ? 'bar-live' : ''}" style="width: ${Math.round((revs[i] / maxRev) * 100)}%"></div></div>
          <span class="bar-val-label">${rupees(revs[i])}</span>
          <span class="bar-status-tag">${isLive ? 'Collected So Far' : `${(this.orders[date] || []).length} orders`}</span>
        </div>`;
    }).join('');
    this.historicalRevenueBars.querySelectorAll('.history-bar-row').forEach((row) => row.addEventListener('click', () => {
      this.selectedDate = row.dataset.date;
      this.populateDatePickers();
      this.render();
    }));

    const categoryTotals = {};
    const itemCounts = {};
    counted.forEach((o) => o.items.forEach((it) => {
      const prod = this.products.find((p) => p.id === it.productId);
      const cat = prod ? prod.category : 'Other';
      categoryTotals[cat] = (categoryTotals[cat] || 0) + it.price * it.qty;
      const key = it.variant ? `${it.name} (${it.variant})` : it.name;
      itemCounts[key] = (itemCounts[key] || 0) + it.qty;
    }));

    const catEntries = Object.entries(categoryTotals).sort((a, b) => b[1] - a[1]);
    const maxCat = catEntries.length ? catEntries[0][1] : 1;
    this.analysisCategoryBars.innerHTML = catEntries.slice(0, 6).map(([cat, val]) => `
      <div class="cat-bar-item">
        <div class="cat-bar-header"><span>${esc(cat)}</span><strong>${rupees(val)}</strong></div>
        <div class="cat-bar-track"><div class="cat-bar-fill" style="width:${Math.round((val / maxCat) * 100)}%;"></div></div>
      </div>`).join('') || '<div style="color:var(--text-muted); font-size:0.8rem;">No sales recorded for this day.</div>';

    this.analysisTopItemsList.innerHTML = Object.entries(itemCounts).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([name, qty]) => `
      <div class="top-item-row"><span class="top-item-name">${esc(name)}</span><span class="top-item-qty">${qty} sold</span></div>`).join('')
      || '<div style="color:var(--text-muted); font-size:0.8rem;">No items ordered yet.</div>';
  }

  openRevenueModal() {
    const dayOrders = this.orders[this.selectedDate] || [];
    const counted = dayOrders.filter(countsRevenue);
    const total = revenueOf(dayOrders);
    const upi = counted.filter((o) => o.paymentMethod === 'UPI').reduce((s, o) => s + o.totalAmount, 0);
    const cash = total - upi;
    const pct = (v) => (total ? ` (${Math.round((v / total) * 100)}%)` : '');

    this.revenueModalTitle.textContent = `Revenue Breakdown — ${this.formatDisplayDate(this.selectedDate)}`;
    this.revModalAmount.textContent = rupees(total);
    this.revModalOrders.textContent = dayOrders.length;
    this.revModalAvg.textContent = rupees(counted.length ? Math.round(total / counted.length) : 0);
    this.revModalUPI.textContent = rupees(upi) + pct(upi);
    this.revModalCash.textContent = rupees(cash) + pct(cash);
    this.revModalPillLabel.textContent = this.selectedDate === this.today ? "Today's Revenue So Far" : 'Final Daily Revenue';

    this.revHistoryTbody.innerHTML = Array.from({ length: 7 }, (_, i) => addDays(this.today, -i)).map((d) => {
      const orders = this.orders[d] || [];
      const live = d === this.today;
      return `
        <tr class="${d === this.selectedDate ? 'highlight-row' : ''}">
          <td>${this.formatDisplayDate(d)}</td>
          <td><span class="badge-stock ${live ? 'stock-yellow' : 'stock-green'}">${live ? 'Collected So Far' : 'Final Daily Revenue'}</span></td>
          <td>${orders.length} Orders</td>
          <td style="text-align:right; font-weight:700;">${rupees(revenueOf(orders))}</td>
        </tr>`;
    }).join('');
    this.openModal(this.revenueModal);
  }

  // =========================================================================
  // Notifications view
  // =========================================================================

  renderNotificationsView() {
    const groups = {
      order: ['new_order', 'order_delivered', 'order_dispatched', 'order_cancelled', 'order_rejected', 'payment_received', 'customer_message'],
      stock: ['low_stock', 'out_of_stock', 'product_added'],
      store: ['store_status'],
    };
    const filtered = this.notifTypeFilter === 'all' ? this.notifications : this.notifications.filter((n) => groups[this.notifTypeFilter]?.includes(n.type));
    if (!filtered.length) {
      this.fullNotifList.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-muted);">No notifications in this category yet.</div>';
      return;
    }
    this.fullNotifList.innerHTML = filtered.map((n) => `
      <div class="full-notif-item ${!n.isRead ? 'unread' : ''}" data-id="${n.id}">
        <span class="full-notif-icon">${this.getNotificationIcon(n.type)}</span>
        <div class="full-notif-content">
          <div class="full-notif-top"><span class="full-notif-title">${esc(n.title)}</span><span class="full-notif-time">${n.time}</span></div>
          <div class="full-notif-msg">${esc(n.message)}</div>
        </div>
      </div>`).join('');
    this.fullNotifList.querySelectorAll('.full-notif-item').forEach((el) => el.addEventListener('click', () => this.openNotification(el.dataset.id)));
  }

  // =========================================================================
  // Settings
  // =========================================================================

  populateSettingsTab() {
    const standardTypes = ['Kirana / General Store', 'Restaurant', 'Café', 'Bakery', 'Medical Shop', 'Grocery Store', 'Clothing Store', 'Electronics Store'];
    this.settingsStoreName.value = this.store.name || '';
    if (standardTypes.includes(this.store.businessType)) {
      this.settingsBusinessType.value = this.store.businessType;
      this.settingsCustomBusinessTypeWrap.style.display = 'none';
      this.settingsCustomBusinessType.value = '';
    } else {
      this.settingsBusinessType.value = 'Other';
      this.settingsCustomBusinessTypeWrap.style.display = 'block';
      this.settingsCustomBusinessType.value = this.store.customBusinessType || (this.store.businessType !== 'Other' ? this.store.businessType : '');
    }
    this.settingsStorePhone.value = this.store.phone || '';
    this.settingsOpeningTime.value = this.store.openingTime || '07:00';
    this.settingsClosingTime.value = this.store.closingTime || '22:00';
    this.settingsStoreAddress.value = this.store.address || '';
    this.settingsStoreCity.value = this.store.city || '';
    this.settingsStorePincode.value = this.store.pincode || '';
    this.locationStatusText.textContent = this.store.coordinates
      ? `Coordinates: ${this.store.coordinates}` : 'No coordinates saved yet — use "Use Current Location".';
    this.settingsAiGreeting.value = this.store.welcomeMessage || '';
    this.settingsDeliveryEta.value = this.store.deliveryEta || '';
    this.settingsUpiId.value = this.store.upiId || '';
    this.settingsDeliveryFee.value = this.store.deliveryFee ?? '';
    this.settingsFreeDeliveryAbove.value = this.store.freeDeliveryAbove ?? '';
    this.settingsMinOrderVal.value = this.store.minOrder ?? '';
  }

  async handleSaveSettingsTab() {
    const bType = this.settingsBusinessType.value;
    const customBType = this.settingsCustomBusinessType.value.trim();
    const errEl = document.getElementById('errSettingsCustomBusinessType');
    if (bType === 'Other' && !customBType) {
      if (errEl) errEl.textContent = 'Please enter your business type.';
      return;
    }
    if (errEl) errEl.textContent = '';
    const upi = this.settingsUpiId.value.trim();
    if (upi && !/^[\w.\-]{2,}@[\w]{2,}$/.test(upi)) {
      this.showToastNotification('⚠️ UPI ID looks invalid (e.g. mystore@okaxis).', true);
      return;
    }
    const num = (el, fallback) => String(Math.max(0, parseFloat(el.value) || 0) || fallback);
    const next = {
      ...this.store,
      name: this.settingsStoreName.value.trim() || this.store.name,
      businessType: bType, customBusinessType: bType === 'Other' ? customBType : '',
      phone: this.settingsStorePhone.value.trim(),
      openingTime: this.settingsOpeningTime.value, closingTime: this.settingsClosingTime.value,
      address: this.settingsStoreAddress.value.trim() || this.store.address,
      city: this.settingsStoreCity.value.trim(), pincode: this.settingsStorePincode.value.trim(),
      coordinates: this.pendingCoordinates || this.store.coordinates,
    };
    try {
      this.settings = await api.put('/api/settings', {
        ...storeToSettings(next),
        welcome_message: this.settingsAiGreeting.value.trim(),
        delivery_eta: this.settingsDeliveryEta.value.trim() || '30-45 mins',
        upi_id: upi || this.store.upiId,
        delivery_fee: num(this.settingsDeliveryFee, '0'),
        free_delivery_above: num(this.settingsFreeDeliveryAbove, '0'),
        min_order: num(this.settingsMinOrderVal, '0'),
      });
      this.store = mapStore(this.settings);
      this.pendingCoordinates = null;
      this.render();
      this.populateSettingsTab();
      this.me = await api.get('/api/auth/me');
      this.renderAccount();
      this.showToastNotification('Settings saved — the WhatsApp bot uses them right away.');
    } catch (e) { this.showToastNotification(`⚠️ ${e.message}`, true); }
  }

  // =========================================================================
  // New-order toast
  // =========================================================================

  showFloatingOrderToast(order) {
    this.activeFloatingOrder = order;
    this.toastOrderNumber.textContent = `Order ${order.orderNumber}`;
    this.toastCustomerName.textContent = order.customerName;
    this.toastCustomerPhone.textContent = order.customerPhone;
    this.toastCustomerAddress.textContent = order.customerAddress;
    this.toastItemsSummary.textContent = order.items.map((it) => `${it.qty} × ${it.name}`).join(', ');
    this.toastTotalAmount.textContent = `${rupees(order.totalAmount)} · ${order.paymentMethod === 'UPI' ? 'UPI' : 'COD'}`;
    this.floatingOrderToast.style.display = 'block';
    clearTimeout(this.incomingOrderToastTimer);
    this.incomingOrderToastTimer = setTimeout(() => this.hideFloatingOrderToast(), 25000);
  }

  hideFloatingOrderToast() {
    this.floatingOrderToast.style.display = 'none';
    this.activeFloatingOrder = null;
  }

  // =========================================================================
  // Customer Chats (store ↔ customer)
  // =========================================================================

  renderChatList() {
    const q = (this.chatSearchInput.value || '').toLowerCase();
    const list = this.customers.filter((c) => !q || `${c.name || ''} ${c.wa_name || ''} ${c.phone}`.toLowerCase().includes(q));
    if (!list.length) {
      this.chatList.innerHTML = '<div class="chat-empty small">No conversations yet. Message the WhatsApp number or use the Bot Preview.</div>';
      return;
    }
    this.chatList.innerHTML = list.map((c) => {
      const name = c.name || c.wa_name || c.phone;
      const ts = c.last_message_at ? new Date(c.last_message_at).getTime() : 0;
      return `
        <button class="chat-list-item ${c.id === this.selectedChatId ? 'active' : ''}" data-chat-id="${c.id}">
          <span class="chat-avatar">${esc(initials(name))}</span>
          <span class="chat-list-body">
            <span class="chat-list-top">
              <strong>${esc(name)}</strong>
              <small>${ts ? timeAgo(ts) : ''}</small>
            </span>
            <span class="chat-list-preview">${esc((c.last_message || '').replace(/[*_]/g, ''))}</span>
            <span class="chat-tags">
              ${c.needs_attention ? '<span class="chat-tag warn">needs reply</span>' : ''}
              ${c.bot_paused ? '<span class="chat-tag off">bot paused</span>' : ''}
              ${c.channel === 'sim' ? '<span class="chat-tag">simulator</span>' : '<span class="chat-tag wa">WhatsApp</span>'}
              ${c.order_count ? `<span class="chat-tag">${c.order_count} orders</span>` : ''}
            </span>
          </span>
        </button>`;
    }).join('');
  }

  async openChat(customerId) {
    if (!customerId) return;
    this.selectedChatId = customerId;
    this.renderChatList();
    let c, msgs;
    try {
      [c, msgs] = await Promise.all([api.get(`/api/customers/${customerId}`), api.get(`/api/customers/${customerId}/messages`)]);
    } catch (e) { return this.showToastNotification(`⚠️ ${e.message}`, true); }
    const name = c.name || c.wa_name || c.phone;
    const cart = c.cart.items.length
      ? `🛒 In cart: ${c.cart.items.map((i) => `${i.quantity}× ${esc(i.item)}`).join(', ')} — <strong>${rupees(c.cart.total)}</strong>`
      : '🛒 Cart is empty';
    this.chatThreadCol.innerHTML = `
      <div class="chat-thread-head">
        <div>
          <div class="chat-thread-name">${esc(name)}</div>
          <div class="chat-thread-meta">${esc(c.phone.startsWith('sim-') ? 'Simulator customer' : '+' + c.phone)} · ${esc(c.address || 'no address yet')} · ${c.orders.length} orders</div>
        </div>
        <div class="chat-thread-actions">
          ${c.needs_attention ? '<button class="btn btn-outline btn-sm" id="chatResolveBtn">✓ Mark resolved</button>' : ''}
          <label class="bot-switch" title="When off, only staff replies — the bot stays quiet">
            <input type="checkbox" id="chatBotToggle" ${c.bot_paused ? '' : 'checked'}>
            <span class="bot-switch-track"></span><span>Bot ${c.bot_paused ? 'paused' : 'on'}</span>
          </label>
        </div>
      </div>
      <div class="chat-cart-strip">${cart}</div>
      <div class="chat-thread" id="chatThread">${msgs.map((m) => this.chatBubbleHtml(m)).join('')}</div>
      <form class="chat-reply-bar" id="chatReplyForm">
        <input type="text" class="form-input" id="chatReplyInput" placeholder="Reply as ${esc(this.store.name)} staff…" autocomplete="off" />
        <button class="btn btn-primary">Send</button>
      </form>`;
    const thread = document.getElementById('chatThread');
    thread.scrollTop = thread.scrollHeight;

    document.getElementById('chatBotToggle').addEventListener('change', async (e) => {
      await api.post(`/api/customers/${customerId}/bot`, { paused: !e.target.checked });
      this.showToastNotification(e.target.checked ? 'Bot resumed for this customer.' : 'You took over — the bot is paused for this customer.');
      await this.loadCustomers();
      this.openChat(customerId);
    });
    document.getElementById('chatResolveBtn')?.addEventListener('click', async () => {
      await api.post(`/api/customers/${customerId}/resolve`);
      await this.loadCustomers();
      this.renderBadges();
      this.openChat(customerId);
    });
    document.getElementById('chatReplyForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('chatReplyInput');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      try { await api.post(`/api/customers/${customerId}/messages`, { text }); }
      catch (err) { this.showToastNotification(`⚠️ ${err.message}`, true); input.value = text; }
    });
  }

  chatBubbleHtml(m) {
    if (m.kind === 'alert') return `<div class="chat-bubble alert">${esc(m.body)}</div>`;
    const who = m.direction === 'out' ? ({ agent: '🤖 Bot', store: '🧑 Staff', system: '🔔 Update' }[m.sender] || '') : '';
    const cls = m.direction === 'out' ? `out ${m.sender}` : 'in';
    let meta = null;
    try { meta = typeof m.meta === 'string' ? JSON.parse(m.meta) : m.meta; } catch {}
    const opts = meta?.options?.length
      ? `<div class="chat-options">${meta.options.map((o) => `<span>${meta.type === 'checklist' ? '☐ ' : ''}${esc(o.title)}</span>`).join('')}</div>` : '';
    const time = new Date(m.created_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    return `<div class="chat-bubble ${cls}">${waToHtml(m.body)}${opts}<span class="chat-bubble-meta">${who ? `${who} · ` : ''}${time}</span></div>`;
  }

  appendChatBubble(m) {
    const thread = document.getElementById('chatThread');
    if (!thread) return;
    thread.insertAdjacentHTML('beforeend', this.chatBubbleHtml(m));
    thread.scrollTop = thread.scrollHeight;
  }

  // =========================================================================
  // WhatsApp Bot Preview — a real conversation with the live bot
  // =========================================================================

  async openSimulator() {
    const inStock = this.products.filter((p) => p.isAvailable && p.quantity > 0);
    const picks = [...new Set(inStock.map((p) => p.name))].sort(() => Math.random() - 0.5).slice(0, 2);
    document.getElementById('waProductPrompts').innerHTML = picks.length
      ? picks.map((n) => `<button class="btn-prompt" data-prompt="${esc(n)}">"${esc(n)}"</button>`).join('')
      : '<span class="prompt-hint">Add products in Inventory to try ordering</span>';
    this.openModal(this.whatsappSimulatorModal);
    this.waChatMessages.innerHTML = '';
    try {
      const customer = (await api.get('/api/customers')).find((c) => c.phone === this.simPhone);
      if (customer) {
        const msgs = await api.get(`/api/customers/${customer.id}/messages?limit=60`);
        msgs.filter((m) => m.kind !== 'alert').forEach((m) => this.appendWaMessage(m.direction === 'in' ? 'customer' : 'bot', m, false));
      }
    } catch {}
    if (!this.waChatMessages.children.length) this.appendSystemNote('This is the live bot — say "Hi" 👋');
    this.waChatMessages.scrollTop = this.waChatMessages.scrollHeight;
    this.waCustomerInput.focus();
  }

  async simSend(payload) {
    if (payload.text) this.appendWaMessage('customer', { body: payload.text, created_at: new Date().toISOString() });
    this.setSimTyping(true);
    try {
      await api.post('/api/sim/message', { phone: this.simPhone, name: this.simName, ...payload });
    } catch (e) {
      this.setSimTyping(false);
      this.appendSystemNote(`⚠️ ${e.message}`);
    }
  }

  handleSendWhatsAppMessage() {
    const text = this.waCustomerInput.value.trim();
    if (!text) return;
    this.waCustomerInput.value = '';
    this.simSend({ text });
  }

  onSimOptionClick(e) {
    const submit = e.target.closest('[data-sim-submit]');
    if (submit) {
      const ids = [...submit.closest('.wa-options').querySelectorAll('input:checked')].map((i) => i.value);
      if (!ids.length) return this.showToastNotification('Tick at least one item', true);
      submit.disabled = true;
      return this.simSend({ text: `🛒 Selected ${ids.length} item(s)`, reply_id: `multi:${ids.join(',')}` });
    }
    const opt = e.target.closest('[data-sim-reply]');
    if (opt) this.simSend({ text: opt.dataset.title, reply_id: opt.dataset.simReply });
  }

  setSimTyping(on) {
    let el = document.getElementById('waTyping');
    if (on && !el) {
      this.waChatMessages.insertAdjacentHTML('beforeend', '<div class="wa-msg wa-bot" id="waTyping"><div class="wa-bubble wa-typing"><span></span><span></span><span></span></div></div>');
      this.waChatMessages.scrollTop = this.waChatMessages.scrollHeight;
    } else if (!on && el) el.remove();
  }

  appendSystemNote(text) {
    this.waChatMessages.insertAdjacentHTML('beforeend', `<div class="wa-system-note">${esc(text)}</div>`);
  }

  appendWaMessage(sender, m, scroll = true) {
    const time = new Date(m.created_at || Date.now()).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    let meta = null;
    try { meta = typeof m.meta === 'string' ? JSON.parse(m.meta) : m.meta; } catch {}
    let options = '';
    if (sender === 'bot' && meta?.options?.length) {
      if (meta.type === 'checklist') {
        options = `<div class="wa-options checklist"><div class="wa-options-label">☑️ ${esc(meta.button)}</div>
          ${meta.options.map((o) => `<label class="wa-check"><input type="checkbox" value="${esc(o.id)}">
            <span>${esc(o.title)}${o.description ? `<small>${esc(o.description)}</small>` : ''}</span></label>`).join('')}
          <button class="wa-option-btn primary" data-sim-submit>Add to cart</button></div>`;
      } else {
        options = `<div class="wa-options ${meta.type}">${meta.type === 'list' ? `<div class="wa-options-label">☰ ${esc(meta.button)}</div>` : ''}
          ${meta.options.map((o) => `<button class="wa-option-btn" data-sim-reply="${esc(o.id)}" data-title="${esc(o.title)}">
            <span>${esc(o.title)}</span>${o.description ? `<small>${esc(o.description)}</small>` : ''}</button>`).join('')}</div>`;
      }
    }
    const label = sender === 'bot' ? `<span class="wa-sender">${esc({ store: '🧑 Store staff', system: '🔔 Order update' }[m.sender] || this.store.name)}</span>` : '';
    this.waChatMessages.insertAdjacentHTML('beforeend', `
      <div class="wa-msg wa-${sender}">
        <div class="wa-bubble">${label}<p>${waToHtml(m.body)}</p>${options}<span class="wa-time">${time}</span></div>
      </div>`);
    if (scroll) this.waChatMessages.scrollTop = this.waChatMessages.scrollHeight;
  }

  // =========================================================================
  // Helpers
  // =========================================================================

  openModal(modalEl) { if (modalEl) modalEl.style.display = 'flex'; }
  closeModal(modalEl) { if (modalEl) modalEl.style.display = 'none'; }

  formatDisplayDate(dateStr) {
    if (!dateStr) return '';
    const label = new Date(`${dateStr}T12:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'long', year: 'numeric' });
    if (dateStr === this.today) return `${label} (Today)`;
    if (dateStr === addDays(this.today, -1)) return `${label} (Yesterday)`;
    return label;
  }

  showToastNotification(msg, isError = false) {
    document.getElementById('miniSysToast')?.remove();
    const toast = document.createElement('div');
    toast.id = 'miniSysToast';
    toast.className = `mini-sys-toast ${isError ? 'is-error' : ''}`;
    toast.innerHTML = `<span>${isError ? '' : '✓'}</span> <span>${esc(msg)}</span>`;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, isError ? 4500 : 2800);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  window.vyaparApp = new VyaparStoreApp();
});
