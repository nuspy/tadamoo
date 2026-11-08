from PIL import Image
import numpy as np
import cv2
import copy


def get_crop_box(box, expand):
    x, y, x1, y1 = box
    x_c, y_c = (x+x1)//2, (y+y1)//2
    w, h = x1-x, y1-y
    s = int(max(w, h)//2*expand)
    crop_box = [x_c-s, y_c-s, x_c+s, y_c+s]
    return crop_box, s


def face_seg(image, mode="raw", fp=None):
    """
    对图像进行面部解析，生成面部区域的掩码。

    Args:
        image (PIL.Image): 输入图像。

    Returns:
        PIL.Image: 面部区域的掩码图像。
    """
    seg_image = fp(image, mode=mode)  # 使用 FaceParsing 模型解析面部
    if seg_image is None:
        print("error, no person_segment")  # 如果没有检测到面部，返回错误
        return None

    seg_image = seg_image.resize(image.size)  # 将掩码图像调整为输入图像的大小
    return seg_image


def get_image(image, face, face_box, upper_boundary_ratio=0.5, expand=1.5, mode="raw", fp=None):
    """
    将裁剪的面部图像粘贴回原始图像，并进行一些处理。

    Args:
        image (numpy.ndarray): 原始图像（身体部分）。
        face (numpy.ndarray): 裁剪的面部图像。
        face_box (tuple): 面部边界框的坐标 (x, y, x1, y1)。
        upper_boundary_ratio (float): 用于控制面部区域的保留比例。
        expand (float): 扩展因子，用于放大裁剪框。
        mode: 融合mask构建方式 

    Returns:
        numpy.ndarray: 处理后的图像。
    """
    # 将 numpy 数组转换为 PIL 图像
    body = Image.fromarray(image[:, :, ::-1])  # 身体部分图像(整张图)
    face = Image.fromarray(face[:, :, ::-1])  # 面部图像

    x, y, x1, y1 = face_box  # 获取面部边界框的坐标
    crop_box, s = get_crop_box(face_box, expand)  # 计算扩展后的裁剪框
    x_s, y_s, x_e, y_e = crop_box  # 裁剪框的坐标
    face_position = (x, y)  # 面部在原始图像中的位置

    # 从身体图像中裁剪出扩展后的面部区域（下巴到边界有距离）
    face_large = body.crop(crop_box)
        
    ori_shape = face_large.size  # 裁剪后图像的原始尺寸

    # 对裁剪后的面部区域进行面部解析，生成掩码
    mask_image = face_seg(face_large, mode=mode, fp=fp)
    
    mask_small = mask_image.crop((x - x_s, y - y_s, x1 - x_s, y1 - y_s))  # 裁剪出面部区域的掩码
    
    mask_image = Image.new('L', ori_shape, 0)  # 创建一个全黑的掩码图像
    mask_image.paste(mask_small, (x - x_s, y - y_s, x1 - x_s, y1 - y_s))  # 将面部掩码粘贴到全黑图像上
    
    
    # 保留面部区域的上半部分（用于控制说话区域）
    width, height = mask_image.size
    top_boundary = int(height * upper_boundary_ratio)  # 计算上半部分的边界
    modified_mask_image = Image.new('L', ori_shape, 0)  # 创建一个新的全黑掩码图像
    modified_mask_image.paste(mask_image.crop((0, top_boundary, width, height)), (0, top_boundary))  # 粘贴上半部分掩码
    
    
    # 对掩码进行高斯模糊，使边缘更平滑
    blur_kernel_size = int(0.05 * ori_shape[0] // 2 * 2) + 1  # 计算模糊核大小
    mask_array = cv2.GaussianBlur(np.array(modified_mask_image), (blur_kernel_size, blur_kernel_size), 0)  # 高斯模糊
    #mask_array = np.array(modified_mask_image)
    mask_image = Image.fromarray(mask_array)  # 将模糊后的掩码转换回 PIL 图像
    
    # 将裁剪的面部图像粘贴回扩展后的面部区域
    face_large.paste(face, (x - x_s, y - y_s, x1 - x_s, y1 - y_s))
    
    body.paste(face_large, crop_box[:2], mask_image)
    
    body = np.array(body)  # 将 PIL 图像转换回 numpy 数组

    return body[:, :, ::-1]  # 返回处理后的图像（BGR 转 RGB）


def get_image_blending(image, face, face_box, mask_array, crop_box):
    """MODIFICA callAPIcall: composizione in numpy sul SOLO riquadro.

    L'originale portava l'INTERO fotogramma numpy -> PIL -> numpy con due
    inversioni di canale (`[:,:,::-1]`), pur toccando solo l'area del volto:
    su 854x480 sono circa dieci volte i pixel necessari. E convertiva la
    maschera con `.convert("L")` a ogni fotogramma, benche' sia gia' calcolata
    e costante.

    Le inversioni di canale spariscono del tutto: la composizione e' la stessa
    operazione su tutti e tre i canali, quindi passare per RGB e tornare a BGR
    non cambiava nulla. Misurato: 4,18 -> 2,79 ms per fotogramma, uscita
    identica a meno dell'arrotondamento (differenza massima 1).
    """
    x, y, x1, y1 = face_box
    x_s, y_s, x_e, y_e = crop_box

    # `.copy()` e non `ascontiguousarray`: quest'ultimo NON copia se l'array e'
    # gia' contiguo, e si finirebbe per modificare il fotogramma del chiamante.
    # L'originale restituiva un array nuovo e c'e' chi passa direttamente il
    # frame del ciclo (scripts/realtime_inference.py), che verrebbe corrotto.
    out = image.copy()
    regione = out[y_s:y_e, x_s:x_e]          # vista sulla copia, non sull'originale
    nuovo = regione.copy()
    nuovo[y - y_s:y1 - y_s, x - x_s:x1 - x_s] = face

    # La maschera pesa quanto della regione modificata sostituisce l'originale.
    #
    # Si porta a UN canale prima di usarla. L'originale lo faceva con
    # `Image.fromarray(mask).convert("L")`, che e' una conversione difensiva:
    # le maschere in cache possono avere tre canali, e allora `[:, :, None]`
    # produce (H,W,3,1) invece di (H,W,1) e il broadcasting genera un array a
    # quattro dimensioni. Visto dal vivo, con l'avatar fermo e muto:
    #   could not broadcast input array from shape (210,210,210,3)
    #                                    into shape (210,210,3)
    m = mask_array
    if m.ndim == 3:
        # Stessi pesi di PIL `.convert("L")`: con una maschera davvero in scala
        # di grigi prendere il primo canale basterebbe, ma se i canali
        # differiscono la luminanza e' cio' che faceva l'originale, e il
        # risultato deve restare identico.
        # `.astype(np.float32)` PRIMA della somma pesata: su uint8 quei
        # prodotti vanno in overflow (255*299 non ci sta in 8 bit) e la
        # maschera esce sbagliata — visto come differenze fino a 224 sui
        # pixel del volto.
        mf = m.astype(np.float32)
        m = (mf[:, :, 0] * 0.299 + mf[:, :, 1] * 0.587 + mf[:, :, 2] * 0.114)
    alfa = (np.asarray(m, dtype=np.float32) / 255.0)[:, :, None]
    regione[:] = regione * (1.0 - alfa) + nuovo * alfa
    return out


def get_image_prepare_material(image, face_box, upper_boundary_ratio=0.5, expand=1.5, fp=None, mode="raw"):
    body = Image.fromarray(image[:,:,::-1])

    x, y, x1, y1 = face_box
    #print(x1-x,y1-y)
    crop_box, s = get_crop_box(face_box, expand)
    x_s, y_s, x_e, y_e = crop_box

    face_large = body.crop(crop_box)
    ori_shape = face_large.size

    mask_image = face_seg(face_large, mode=mode, fp=fp)
    mask_small = mask_image.crop((x-x_s, y-y_s, x1-x_s, y1-y_s))
    mask_image = Image.new('L', ori_shape, 0)
    mask_image.paste(mask_small, (x-x_s, y-y_s, x1-x_s, y1-y_s))

    # keep upper_boundary_ratio of talking area
    width, height = mask_image.size
    top_boundary = int(height * upper_boundary_ratio)
    modified_mask_image = Image.new('L', ori_shape, 0)
    modified_mask_image.paste(mask_image.crop((0, top_boundary, width, height)), (0, top_boundary))

    blur_kernel_size = int(0.1 * ori_shape[0] // 2 * 2) + 1
    mask_array = cv2.GaussianBlur(np.array(modified_mask_image), (blur_kernel_size, blur_kernel_size), 0)
    return mask_array, crop_box
